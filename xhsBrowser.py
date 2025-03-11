import sys
import json
import hashlib
import time
import logging
import os

from bs4 import BeautifulSoup
from PyQt5.QtCore import QUrl, Qt, pyqtSignal, pyqtSlot, QTimer, QObject
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile, QWebEngineSettings
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QLineEdit, QPushButton, QTabWidget, QHBoxLayout,
    QTabBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QDialog, QTextBrowser, QProgressBar,
    QMessageBox, QLabel, QScrollArea, QFrame,
    QGridLayout, QSizePolicy
)
from PyQt5.QtWebChannel import QWebChannel
from urllib.parse import urljoin
from pydantic import BaseModel, HttpUrl, Field

# 配置日志，设置日志级别为 ERROR，日志格式包含时间、级别和消息
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

# 提取设置用户代理的方法
def set_user_agent(profile):
    """
    设置用户代理，模拟浏览器访问
    :param profile: QWebEngineProfile 对象
    """
    profile.setHttpUserAgent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )

# 定义笔记项的数据模型
class NoteItem(BaseModel):
    """
    笔记项数据模型，使用 pydantic 定义
    """
    id: str  # 笔记 ID
    title: str = Field(..., max_length=100)  # 笔记标题，最大长度为 100
    url: HttpUrl  # 笔记链接
    author: str  # 笔记作者
    author_link: HttpUrl  # 作者主页链接
    likes: int = Field(ge=0)  # 笔记点赞数，必须大于等于 0
    cover: str  # 笔记封面
    images: list[str]  # 笔记中的图片列表
    timestamp: int  # 笔记时间戳

# 自动滚动控制器类
class AutoScrollController:
    """
    自动滚动控制器，用于自动滚动网页并触发数据收集
    """
    def __init__(self, web_view, callback):
        """
        初始化自动滚动控制器
        :param web_view: QWebEngineView 对象，用于加载网页
        :param callback: 回调函数，用于触发数据收集
        """
        self.web_view = web_view
        self.callback = callback
        self.timer = QTimer()  # 定时器，用于定时滚动
        self.timer.setInterval(3000)  # 设置定时器间隔为 3 秒
        self.last_height = 0  # 记录上一次滚动的高度
        self.retry_count = 0  # 重试次数
        self.active = False  # 滚动控制器是否激活

    def start(self):
        """
        启动自动滚动
        """
        if not self.active:
            self.active = True
            self.timer.timeout.connect(self._scroll_step)  # 定时器超时后调用滚动步骤方法
            self.timer.start()

    def stop(self):
        """
        停止自动滚动
        """
        if self.active:
            self.active = False
            self.timer.stop()  # 停止定时器

    def _scroll_step(self):
        """
        执行滚动步骤，等待页面加载完成后滚动到底部
        """
        js = """
        (function() {
            // 等待所有图片和异步内容加载完成
            const waitForLoad = () => {
                const images = Array.from(document.images);
                const pendingImages = images.filter(img => !img.complete);
                
                return Promise.all(pendingImages.map(img => 
                    new Promise(resolve => {
                        img.onload = img.onerror = resolve;
                    })
                )).then(() => {
                    window.scrollTo(0, document.body.scrollHeight);
                    return new Promise(resolve => {
                        setTimeout(() => {
                            resolve({
                                height: document.body.scrollHeight,
                                item_count: document.querySelectorAll('.note-item').length,
                                loading: !!document.querySelector('.loading-indicator')
                            });
                        }, 1000);
                    });
                });
            };
            
            return waitForLoad();
        })();
        """
        self.web_view.page().runJavaScript(js, self._handle_scroll)  # 执行 JavaScript 代码并处理结果

    def _handle_scroll(self, result):
        """
        处理滚动结果，根据结果决定是否继续滚动或停止
        :param result: 滚动结果，包含页面高度、笔记项数量和加载状态
        """
        #print("result", result)
        self.callback()
        if result == {}:
            return

        if result.get('loading', False):
            QTimer.singleShot(1500, self._scroll_step)  # 如果页面正在加载，等待 1.5 秒后重试
            return

# 网页通信类，用于与 JavaScript 通信
class WebComm(QObject):
    """
    网页通信类，用于与 JavaScript 通信，触发内容处理
    """
    # 定义一个信号，用于触发内容处理
    contentCaptured = pyqtSignal(str)

    def __init__(self, callback):
        """
        初始化网页通信类
        :param callback: 回调函数，用于接收 HTML 内容
        """
        super().__init__()
        self.callback = callback  # 接收 HTML 内容的回调函数

    @pyqtSlot()
    def capture_trigger(self):
        """
        当接收到 JavaScript 通知时，调用回调函数并发射信号
        """
        # 调用回调函数处理 HTML 内容
        self.callback()
        # 发射信号
        self.contentCaptured.emit('newContent')

# 浏览器标签页类
class BrowserTab(QWidget):
    """
    浏览器标签页类，用于显示网页并进行数据收集
    """
    dataCaptured = pyqtSignal(dict)  # 数据捕获信号，用于发送收集到的数据
    statusUpdated = pyqtSignal(str)  # 状态更新信号，用于更新状态栏消息

    def __init__(self, parent=None):
        """
        初始化浏览器标签页
        :param parent: 父窗口对象
        """
        super().__init__(parent)
        self.collected_notes = set()  # 已收集的笔记 ID 集合
        self.scroll_controller = None  # 自动滚动控制器
        self.status_checker = QTimer()  # 状态检查定时器
        self._init_ui()  # 初始化界面
        self._init_web_engine()  # 初始化网页引擎



        # 调整布局以并排显示两个 WebView
        main_layout = QHBoxLayout()
        left_layout = QVBoxLayout()
        left_layout.addWidget(self.control_frame)
        left_layout.addWidget(self.status_label)
        left_layout.addWidget(self.web_view)
        main_layout.addLayout(left_layout)



        self.setLayout(main_layout)

        self.comm = WebComm(self.parse_html)  # 创建网页通信对象
        self.channel = QWebChannel()  # 创建 Web 通道
        self.channel.registerObject('webComm', self.comm)  # 注册网页通信对象到 Web 通道
        self.web_view.page().setWebChannel(self.channel)  # 设置 Web 通道到网页

        # 连接 WebComm 的 contentCaptured 信号到 handle_new_content 方法
        self.comm.contentCaptured.connect(self.handle_new_content)

        self._init_signals()  # 初始化信号和槽

    def _init_ui(self):
        """
        初始化用户界面
        """
        self.control_frame = QFrame()
        self.control_frame.setFrameShape(QFrame.StyledPanel)
        control_layout = QGridLayout(self.control_frame)

        # 第一行组件
        control_layout.addWidget(QLabel("目标 URL:"), 0, 0)  # 添加标签
        self.url_input = QLineEdit()  # 创建输入框
        self.url_input.setPlaceholderText("输入小红书探索页 URL")  # 设置输入框提示文本
        control_layout.addWidget(self.url_input, 0, 1)  # 添加输入框到布局

        # 第二行组件
        self.toggle_btn = QPushButton("正在监控")  # 创建按钮
        control_layout.addWidget(self.toggle_btn, 1, 0)  # 添加按钮到布局

        self.progress = QProgressBar()  # 创建进度条
        self.progress.setTextVisible(False)  # 隐藏进度条百分比文本
        control_layout.addWidget(self.progress, 1, 1)  # 添加进度条到布局

        # 状态标签
        self.status_label = QLabel("就绪")  # 创建状态标签
        self.status_label.setAlignment(Qt.AlignCenter)  # 设置标签居中对齐
        self.status_label.setStyleSheet("color: #666; font: 9pt;")  # 设置标签样式

        # 浏览器视图
        self.web_view = QWebEngineView()  # 创建浏览器视图
        self.web_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # 设置浏览器视图大小策略

    def _init_web_engine(self):
        profile = QWebEngineProfile.defaultProfile()
        self.web_view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)  # 启用 JavaScript
        self.web_view.settings().setAttribute(QWebEngineSettings.LocalStorageEnabled, True)  # 启用本地存储
        self.web_view.load(QUrl("https://www.xiaohongshu.com/explore"))  # 加载小红书探索页
        self.scroll_controller = AutoScrollController(self.web_view, self.capture_data)  # 创建自动滚动控制器

    def _init_signals(self):
        """
        初始化信号和槽
        """
        self.toggle_btn.clicked.connect(self.toggle_monitoring)  # 连接按钮点击信号到切换监控状态方法
        self.url_input.returnPressed.connect(self.load_url)  # 连接输入框回车信号到加载 URL 方法
        self.web_view.loadFinished.connect(self.on_page_loaded)  # 连接页面加载完成信号到页面加载完成处理方法
        self.status_checker.timeout.connect(self.check_page_status)  # 连接状态检查定时器超时信号到检查页面状态方法


    def toggle_monitoring(self):
        """
        切换监控状态
        """
        if self.scroll_controller.active:
            self._stop_monitoring()  # 如果正在监控，停止监控
            self.toggle_btn.setText("开始监控")
        else:
            self._start_monitoring()  # 如果未监控，开始监控
            self.toggle_btn.setText("正在监控")

    def _start_monitoring(self):
        """
        开始监控
        """
        url = self.url_input.text().strip() or "https://www.xiaohongshu.com/explore"  # 获取输入的 URL 或使用默认 URL
        self.web_view.load(QUrl(url))  # 加载 URL
        self.toggle_btn.setText("停止监控")  # 修改按钮文本
        self.status_label.setText("初始化页面加载...")  # 更新状态标签文本
        self.progress.setValue(20)  # 设置进度条值为 20

    def _stop_monitoring(self):
        """
        停止监控
        """
        self.scroll_controller.stop()  # 停止自动滚动
        self.status_checker.stop()  # 停止状态检查定时器
        self.toggle_btn.setText("开始监控")  # 修改按钮文本
        self.status_label.setText("监控已停止")  # 更新状态标签文本
        self.progress.setValue(0)  # 设置进度条值为 0

    def load_url(self):
        """
        加载 URL，调用开始监控方法
        """
        self._start_monitoring()

    def on_page_loaded(self, success):
        """
        处理页面加载完成事件
        :param success: 页面是否加载成功
        """
        if success:
            js = """
            document.querySelector('.note-item') ? true : false;
            """
            self.web_view.page().runJavaScript(js, self.handle_content_loaded)  # 执行 JavaScript 代码检查页面是否有笔记项
        else:
            self.status_label.setText("页面加载失败")  # 更新状态标签文本
            self.progress.setValue(0)  # 设置进度条值为 0

    def handle_content_loaded(self, has_content):
        """
        处理页面内容加载完成事件
        :param has_content: 页面是否有笔记项
        """
        if has_content:
            self.status_label.setText("页面内容已加载")  # 更新状态标签文本
            self.progress.setValue(40)  # 设置进度条值为 40
            self.start_data_collection()
        else:
            self.web_view.reload()  # 重新加载页面
            self.status_label.setText("正在重新加载页面...")  # 更新状态标签文本

    def start_data_collection(self):
        """
        开始数据收集
        """
        self.web_view.page().runJavaScript("""
            window.__isLoading = false;
            setInterval(() => {
                const loading = !!document.querySelector('.loading-indicator');
                window.__isLoading = loading;
            }, 500);
        """)  # 执行 JavaScript 代码设置页面加载状态监控

        self.status_checker.start(1000)  # 启动状态检查定时器
        self.scroll_controller.start()  # 启动自动滚动
        self.capture_data()  # 开始捕获数据
        self.status_label.setText("数据收集进行中...")  # 更新状态标签文本
        self.progress.setValue(60)  # 设置进度条值为 60

    def check_page_status(self):
        """
        检查页面状态
        """
        self.web_view.page().runJavaScript("window.__isLoading",
                                           lambda loading: self.status_label.setText(
                                               "页面正在加载..." if loading else "数据收集进行中...")
                                           )  # 执行 JavaScript 代码检查页面加载状态并更新状态标签文本

    def capture_data(self):
        """
        捕获页面数据
        """
        def parse_html_directly(html):
            """
            直接解析 HTML 内容
            :param html: HTML 内容
            """
            self.parse_html(html)

        self.web_view.page().toHtml(parse_html_directly)

    def parse_html(self, html):
        try:
            soup = BeautifulSoup(html, 'html.parser')  # 使用 BeautifulSoup 解析 HTML 内容
            notes = []  # 存储提取的笔记信息
            base_url = "https://www.xiaohongshu.com"  # 基础 URL

            for item in soup.select('section.note-item'):  # 遍历所有笔记项
                try:
                    # 跳过隐藏项
                    if 'display: none' in item.get('style', '') or 'visibility: hidden' in item.get('style', ''):
                        continue

                    # 获取笔记核心 ID
                    note_link = item.select_one('a.cover.mask[href*="xsec_token="]')
                    if not note_link:
                        continue

                    full_url = urljoin(base_url, note_link['href'])  # 拼接完整的笔记链接
                    note_core_id = full_url.split('?')[0].split('/')[-1]  # 提取笔记核心 ID
                    note_id = hashlib.md5(note_core_id.encode()).hexdigest()[:8]  # 生成笔记 ID

                    if note_id in self.collected_notes:
                        continue

                    # 提取笔记信息
                    note_data = self._extract_note_info(item, base_url, note_id, full_url)
                    if note_data:
                        #print(note_data)  # 打印笔记信息
                        self.collected_notes.add(note_id)  # 将笔记 ID 添加到已收集集合
                        notes.append(note_data)  # 将笔记信息添加到列表

                except Exception as e:
                    logging.error(f"解析单个笔记失败: {str(e)}")  # 记录错误日志
                    continue

            if notes:
                self.dataCaptured.emit({'notes': notes})  # 发送数据捕获信号
                self.status_label.setText(f"新增 {len(notes)} 条笔记，共 {len(self.collected_notes)} 条笔记")  # 更新状态标签文本
                self.progress.setValue(100)  # 设置进度条值为 100
                QTimer.singleShot(1500, lambda: self.progress.setValue(80))  # 延迟 1.5 秒后将进度条值设置为 80

        except Exception as e:
            self.handle_html_error(e, "解析 HTML 时出错")

    def _extract_note_info(self, item, base_url, note_id, full_url):
        """
        提取笔记信息
        :param item: 笔记项的 HTML 元素
        :param base_url: 基础 URL
        :param note_id: 笔记 ID
        :param full_url: 笔记完整链接
        :return: 笔记信息字典
        """
        # 作者信息
        author_tag = item.select_one('.author-wrapper .author[href]')  # 选择作者标签
        author_url = urljoin(base_url, author_tag['href']) if author_tag else ""  # 拼接作者主页链接
        author_name = author_tag.select_one('.name').get_text(strip=True) if author_tag else ""  # 获取作者姓名

        # 点赞数处理
        like_text = item.select_one('.like-wrapper .count').get_text(strip=True) if item.select_one(
            '.like-wrapper') else "0"  # 获取点赞数文本
        likes = self._parse_like_count(like_text)  # 解析点赞数

        # 封面图片
        cover_img = item.select_one('.cover.mask img')  # 选择封面图片标签
        cover_url = cover_img['src'] if cover_img else ""  # 获取封面图片链接

        return {
            'id': note_id,  # 笔记 ID
            'title': self._extract_text(item, '.title'),  # 笔记标题
            'url': full_url,  # 笔记链接
            'author': author_name,  # 笔记作者
            'author_link': author_url,  # 作者主页链接
            'likes': likes,  # 笔记点赞数
            'cover': cover_url,  # 笔记封面
            'images': [img['src'] for img in item.select('img[src]')],  # 笔记中的图片列表
            'timestamp': int(time.time())  # 笔记时间戳
        }

    def _parse_like_count(self, text):
        """
        处理包含 '万' 或 '千' 的点赞数
        :param text: 点赞数文本
        :return: 处理后的点赞数
        """
        text = text.replace('+', '').strip()  # 去除多余字符

        try:
            if '万' in text:
                like_count = float(text.replace('万', '')) * 10000  # 处理包含 '万' 的点赞数
            elif '千' in text:
                like_count = float(text.replace('千', '')) * 1000  # 处理包含 '千' 的点赞数
            else:
                like_count = float(text)  # 处理普通点赞数
            return int(like_count)  # 转换为整数
        except ValueError:
            return 0  # 处理异常情况

    def _generate_note_id(self, item):
        """
        生成笔记 ID
        :param item: 笔记项的 HTML 元素
        :return: 笔记 ID
        """
        link = self._extract_attr(item, 'a[href*="xsec_token"]', 'href')  # 提取笔记链接
        return hashlib.md5(link.encode()).hexdigest()[:8] if link else None  # 生成笔记 ID

    @staticmethod
    def _extract_text(soup, selector):
        """
        提取 HTML 元素的文本内容
        :param soup: BeautifulSoup 对象
        :param selector: CSS 选择器
        :return: 提取的文本内容
        """
        elem = soup.select_one(selector)  # 选择 HTML 元素
        return elem.text.strip() if elem else ''  # 返回元素文本内容

    @staticmethod
    def _extract_attr(soup, selector, attr):
        """
        提取 HTML 元素的属性值
        :param soup: BeautifulSoup 对象
        :param selector: CSS 选择器
        :param attr: 属性名
        :return: 提取的属性值
        """
        elem = soup.select_one(selector)  # 选择 HTML 元素
        return elem.get(attr, '') if elem else ''  # 返回元素属性值

    @staticmethod
    def _parse_number(soup, selector):
        """
        解析 HTML 元素中的数字
        :param soup: BeautifulSoup 对象
        :param selector: CSS 选择器
        :return: 解析后的数字
        """
        text = BrowserTab._extract_text(soup, selector)  # 提取元素文本内容
        try:
            return int(text.replace(',', ''))  # 转换为整数
        except ValueError:
            return 0  # 处理异常情况

    def handle_new_content(self, message):
        if message == 'newContent':
            print("New content detected, capturing data...")  # 调试信息
            self.capture_data()  # 如果收到新内容消息，捕获数据


# 笔记表格类
class NotesTable(QTableWidget):
    """
    笔记表格类，用于显示笔记信息
    """
    def __init__(self):
        """
        初始化笔记表格
        """
        super().__init__()
        self._init_table()  # 初始化表格
        self.data = {}  # 存储笔记数据
        self._current_sort = None  # 当前排序信息

    def _init_table(self):
        """
        初始化表格
        """
        self.setColumnCount(8)  # 设置表格列数为 8
        self.setHorizontalHeaderLabels(['ID', '标题', '作者', '作者主页', '笔记链接', '点赞数', '封面图片', '时间'])  # 设置表格表头
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 设置 ID 列自适应内容大小
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)  # 设置标题列自适应拉伸
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)  # 设置作者主页列自适应内容大小
        self.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)  # 设置笔记链接列自适应内容大小
        self.verticalHeader().setVisible(False)  # 隐藏垂直表头
        self.setSortingEnabled(True)  # 启用表格排序
        self.setEditTriggers(QTableWidget.NoEditTriggers)  # 禁用表格编辑
        self.doubleClicked.connect(self.show_detail)  # 连接表格双击信号到显示详情方法

    def update_data(self, notes):
        """
        更新表格数据
        :param notes: 笔记信息列表
        """
        for note in notes:
            if note['id'] in self.data:
                continue

            row = self.rowCount()  # 获取表格当前行数
            self.insertRow(row)  # 插入新行
            self.data[note['id']] = note  # 存储笔记原始数据

            # ID 列
            self.setItem(row, 0, QTableWidgetItem(note['id'][:6]))  # 设置 ID 列内容

            # 标题列
            title_item = QTableWidgetItem(note['title'])  # 创建标题项
            title_item.setToolTip(note['title'])  # 设置标题项鼠标悬停提示
            self.setItem(row, 1, title_item)  # 设置标题列内容

            # 作者列
            self.setItem(row, 2, QTableWidgetItem(note['author']))  # 设置作者列内容

            # 作者主页链接（可点击）
            author_link = QLabel()  # 创建标签
            author_link.setText(f'<a href="{note["author_link"]}">主页</a>')  # 设置标签文本为链接
            author_link.setOpenExternalLinks(True)  # 启用标签链接点击
            self.setCellWidget(row, 3, author_link)  # 设置作者主页列内容为标签

            # 笔记链接（可点击）
            note_link = QLabel()  # 创建标签
            note_link.setText(f'<a href="{note["url"]}">查看</a>')  # 设置标签文本为链接
            note_link.setOpenExternalLinks(True)  # 启用标签链接点击
            self.setCellWidget(row, 4, note_link)  # 设置笔记链接列内容为标签

            # 点赞数
            self.setItem(row, 5, QTableWidgetItem(str(note['likes'])))  # 设置点赞数列内容

            # 封面图片
            cover_label = QLabel()  # 创建标签
            cover_label.setFixedSize(60, 60)  # 设置标签大小
            if note['cover']:
                cover_label.setToolTip(note['cover'])  # 设置标签鼠标悬停提示
                cover_label.setStyleSheet(f"border-image: url({note['cover']}) 0 0 0 0 stretch stretch;")  # 设置标签样式为显示封面图片
            else:
                cover_label.setText("无封面")  # 设置标签文本为无封面
            self.setCellWidget(row, 6, cover_label)  # 设置封面图片列内容为标签

            # 时间
            time_item = QTableWidgetItem(
                time.strftime("%m-%d %H:%M", time.localtime(note['timestamp']))
            )  # 创建时间项
            time_item.setData(Qt.UserRole, note['timestamp'])  # 存储原始时间戳用于排序
            self.setItem(row, 7, time_item)  # 设置时间列内容

        print(f"Updated {len(notes)} notes in the table.")  # 调试信息

    def show_detail(self, index):
        """
        显示笔记详情对话框
        :param index: 表格项索引
        """
        row = index.row()  # 获取表格行号
        note_id = self.item(row, 0).text()  # 获取笔记 ID
        dialog = NoteDetailDialog(self.data[note_id])  # 创建笔记详情对话框
        dialog.exec_()  # 显示对话框

# 笔记详情对话框类
class NoteDetailDialog(QDialog):
    """
    笔记详情对话框类，用于显示笔记详细信息
    """
    def __init__(self, note):
        """
        初始化笔记详情对话框
        :param note: 笔记信息字典
        """
        super().__init__()
        self.note = note  # 存储笔记信息
        self._init_ui()  # 初始化界面

    def _init_ui(self):
        """
        初始化用户界面
        """
        # 添加链接信息到原始布局
        layout = QVBoxLayout()  # 创建垂直布局

        # 链接区域
        link_frame = QFrame()  # 创建框架
        link_layout = QHBoxLayout(link_frame)  # 创建水平布局

        # 作者主页链接
        author_link = QLabel()  # 创建标签
        author_link.setText(f'<b>作者主页:</b> <a href="{self.note["author_link"]}">{self.note["author_link"]}</a>')  # 设置标签文本为作者主页链接
        author_link.setOpenExternalLinks(True)  # 启用标签链接点击

        # 笔记链接
        note_link = QLabel()  # 创建标签
        note_link.setText(f'<b>笔记地址:</b> <a href="{self.note["url"]}">{self.note["url"]}</a>')  # 设置标签文本为笔记链接
        note_link.setOpenExternalLinks(True)  # 启用标签链接点击

        link_layout.addWidget(author_link)  # 添加作者主页链接标签到水平布局
        link_layout.addWidget(note_link)  # 添加笔记链接标签到水平布局

        # 主要信息区域
        info_frame = QFrame()  # 创建框架
        info_layout = QVBoxLayout(info_frame)  # 创建垂直布局
        self.title_label = QLabel(self.note['title'])  # 创建标题标签
        self.author_label = QLabel(self.note['author'])  # 创建作者标签
        self.likes_label = QLabel(str(self.note['likes']))  # 创建点赞数标签
        info_layout.addWidget(self.title_label)  # 添加标题标签到垂直布局
        info_layout.addWidget(self.author_label)  # 添加作者标签到垂直布局
        info_layout.addWidget(self.likes_label)  # 添加点赞数标签到垂直布局

        # 添加到主布局
        layout.addWidget(info_frame)  # 添加主要信息区域框架到主布局
        layout.addWidget(link_frame)  # 添加链接区域框架到主布局
        layout.addWidget(QLabel("图片预览:"))  # 添加图片预览标签到主布局
        # 添加图片预览逻辑
        self.image_scroll = QScrollArea()  # 创建滚动区域
        layout.addWidget(self.image_scroll)  # 添加滚动区域到主布局
        layout.addWidget(QLabel("评论:"))  # 添加评论标签到主布局
        # 添加评论逻辑
        self.comments_table = QTableWidget()  # 创建表格
        layout.addWidget(self.comments_table)  # 添加表格到主布局
        self.setLayout(layout)  # 设置对话框布局

# 主窗口类
class MainWindow(QMainWindow):
    """
    主窗口类，用于显示整个应用程序界面
    """
    def __init__(self):
        """
        初始化主窗口
        """
        super().__init__()
        self.setWindowTitle("小红书实时数据收集系统")  # 设置窗口标题
        self.setGeometry(100, 100, 1200, 900)  # 设置窗口位置和大小
        self._init_ui()  # 初始化界面
        profile = QWebEngineProfile.defaultProfile()  # 获取默认的浏览器配置文件
        set_user_agent(profile)  # 设置用户代理
        export_btn = QPushButton("导出数据", self)  # 创建导出数据按钮
        export_btn.clicked.connect(self.export_data)  # 连接按钮点击信号到导出数据方法
        self.tabs.setCornerWidget(export_btn, Qt.TopRightCorner)  # 将按钮添加到标签页右上角




        self.showMaximized()

    def _init_ui(self):
        """
        初始化用户界面
        """
        self.tabs = QTabWidget()  # 创建标签页
        self.setCentralWidget(self.tabs)  # 设置标签页为中心部件

        self.browser_tab = BrowserTab()  # 创建浏览器标签页
        self.data_table = NotesTable()  # 创建笔记表格

        self.browser_tab.dataCaptured.connect(self.handle_new_data)  # 连接浏览器标签页数据捕获信号到处理新数据方法
        self.browser_tab.statusUpdated.connect(self.show_status_message)  # 连接浏览器标签页状态更新信号到显示状态消息方法

        self.tabs.addTab(self.browser_tab, "数据收集")  # 添加浏览器标签页到标签页
        self.tabs.addTab(self.data_table, "笔记列表")  # 添加笔记表格到标签页

    @pyqtSlot(dict)
    def handle_new_data(self, data):
        """
        处理新收集到的数据
        :param data: 包含笔记信息的字典
        """
        if 'notes' in data:
            self.data_table.update_data(data['notes'])  # 如果数据中包含笔记信息，更新笔记表格
            print(f"Updated {len(data['notes'])} notes in the table.")  # 调试信息

    @pyqtSlot(str)
    def show_status_message(self, message):
        """
        显示状态消息
        :param message: 状态消息文本
        """
        self.statusBar().showMessage(message, 5000)  # 在状态栏显示消息，显示 5 秒

    def export_data(self):
        """
        导出数据到 JSON 文件
        """
        data = []  # 存储要导出的数据
        for note_id, note in self.data_table.data.items():  # 直接从 data 属性中获取笔记数据
            data.append({
                "id": note_id,  # 笔记 ID
                "title": note['title'],  # 笔记标题
                "author": note['author'],  # 笔记作者
                "author_link": note['author_link'],  # 作者主页链接
                "note_link": note['url'],  # 笔记链接
                "likes": note['likes'],  # 笔记点赞数
                "cover": note['cover'],  # 笔记封面
                "timestamp": note['timestamp'],  # 笔记时间戳
            })

        filename = f"notes_{time.strftime('%Y%m%d_%H%M%S')}.json"  # 生成文件名
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)  # 将数据保存到 JSON 文件
            QMessageBox.information(self, "导出成功", f"数据已保存到 {filename}")  # 显示导出成功消息框
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"保存文件时出错: {str(e)}")  # 显示导出失败消息框
            logging.error(f"导出数据失败: {str(e)}")  # 记录错误日志



if __name__ == "__main__":
    app = QApplication(sys.argv)  # 创建应用程序对象
    window = MainWindow()  # 创建主窗口对象
    window.show()  # 显示主窗口
    sys.exit(app.exec_())  # 进入应用程序主循环