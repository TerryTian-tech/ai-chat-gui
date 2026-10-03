"""桌宠组件：一只通体乌黑、金瞳竖瞳的玄猫（半写实画风）。

姿态：端坐摇尾 / 散步 / 犯困点头 / 蜷睡（带 Zzz）/ 伸懒腰，
以及与 AI 任务联动的四种姿态：思考（抬爪抵腮冒问号）/ 干活（前爪刨地）/ 开心（跳跃撒花）/ 失落（飞机耳冒汗）。
平时按随机时长的状态机自然轮换；AI 思考或执行工具时切换到对应动作。

外形贴合玄猫真实照片：又大又圆的橙金眼 + 圆黑大瞳孔（照片里黑猫的标志性眼神）、小而圆钝的
宽位双耳（粉灰耳内）、圆颊白须、暖黑毛色带柔光高光、由粗到细的锥形长尾；端坐与失落姿态的
躯干用一整条轮廓线（背线→圆臀→胸线）配合径向渐变做出无接缝的体积感。
深色剪影加浅描边保证明暗桌面都可见。
动画由单一计时器驱动。
"""

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient, QTransform
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# 配色：玄猫（对照照片：暖黑毛色 + 橙金大圆眼）
FUR_HI = QColor("#4A4248")       # 受光面毛色（照片里黑猫的暖灰高光）
FUR = QColor("#262026")          # 中间调毛色
FUR_DEEP = QColor("#171318")     # 背光面/远侧腿
FUR_EDGE = QColor("#514A50")     # 剪影描边，深色桌面上也能看清轮廓
FUR_SHEEN = QColor("#3A333A")    # 口鼻、胸毛、爪尖的高光块
EAR_INNER = QColor("#55464E")    # 耳内（照片里的暗粉灰）
EYE_GOLD_HI = QColor("#F8D06E")  # 橙金虹膜（照片里的大圆眼）
EYE_GOLD = QColor("#E8A93B")
EYE_GOLD_DEEP = QColor("#B2791D")
PUPIL = QColor("#0D0A10")
NOSE = QColor("#43333A")         # 鼻头（照片里接近黑色的暗棕）
MOUTH = QColor("#8A7B84")        # 嘴线/闭眼线（黑毛上用浅色才可见）
WHISKER = QColor(242, 242, 250, 150)
BLUSH = QColor(235, 130, 140, 38)
COLLAR = QColor("#D95252")       # 红颈带 + 金铃铛
COLLAR_DARK = QColor("#B23F3F")
BELL = QColor("#F2C14E")
SHADOW = QColor(30, 26, 40, 40)
INK = QColor(116, 128, 158)      # ？/Z 等气泡线条色
SPARK = QColor(245, 194, 66)     # 星星
SWEAT = QColor(91, 155, 213)     # 汗滴

POSE_LABELS = {
    "sit": "端坐摇尾巴 🐈‍⬛",
    "walk": "散步中 🐾",
    "drowsy": "犯困中 🥱",
    "sleep": "蜷成一团打盹 😴",
    "stretch": "伸懒腰~ 🐈",
    "think": "思考中… 🤔",
    "work": "努力干活中 💨",
    "happy": "完成啦！🎉",
    "sad": "出了点小状况… 😔",
}

# 平静状态下的轮换序列（秒数区间随机）
CALM_CYCLE = [
    ("sit", (8, 14)),
    ("walk", (2.5, 4.0)),
    ("sit", (5, 9)),
    ("drowsy", (8, 12)),
    ("sleep", (20, 35)),
    ("stretch", (2.2, 2.2)),
]


def _fur_radial(cx, cy, r, hi=FUR_HI, mid=FUR, lo=FUR_DEEP):
    """标准毛色径向渐变（左上受光）"""
    g = QRadialGradient(QPointF(cx, cy), r)
    g.setColorAt(0.0, hi)
    g.setColorAt(0.55, mid)
    g.setColorAt(1.0, lo)
    return QBrush(g)


def _fur_linear(y0, y1, hi=FUR_HI, lo=FUR_DEEP):
    """标准毛色垂直渐变"""
    g = QLinearGradient(0, y0, 0, y1)
    g.setColorAt(0.0, hi)
    g.setColorAt(1.0, lo)
    return QBrush(g)


class CatWidget(QWidget):
    """桌面玄猫组件：点击恢复主窗口，可拖动，右键菜单退出"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(160, 120)

        self._t = 0.0                # 动画时钟（秒）
        self._pose = "sit"
        self._pose_elapsed = 0.0
        self._pose_duration = 10.0
        self._cycle_index = 0
        self._ai_state = "idle"      # AI 任务状态：idle/thinking/working/happy/sad
        self._blink_seed = random.random() * 10

        # 拖动 / 点击状态
        self._drag_active = False
        self._drag_moved = False
        self._drag_offset = None

        self._enter_pose("sit", random.uniform(6, 10))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # 不在 __init__ 启动：桌宠初始不可见，由 showEvent 按需启动，
        # 避免主窗口常开时每 33ms 空转唤醒一次

    # ---------- 对外接口 ----------

    def set_ai_state(self, state: str):
        """AI 任务状态联动。

        - thinking / working：持续保持对应动作，直到下一次状态更新
        - happy / sad：播放一段庆祝/失落后自动回到平静轮换
        - idle：回到平静轮换
        """
        self._ai_state = state
        if state == "thinking":
            self._enter_pose("think", 1e9)
        elif state == "working":
            self._enter_pose("work", 1e9)
        elif state == "happy":
            self._cycle_index = 0
            self._enter_pose("happy", 2.8)
        elif state == "sad":
            self._cycle_index = 0
            self._enter_pose("sad", 2.6)
        else:  # idle
            self._cycle_index = 0
            self._enter_pose("stretch", 1.6)

    # ---------- 状态机 ----------

    def _enter_pose(self, pose: str, duration: float):
        self._pose = pose
        self._pose_elapsed = 0.0
        self._pose_duration = duration
        self.setToolTip(f"玄猫：{POSE_LABELS.get(pose, pose)}")

    def _next_calm_pose(self):
        pose, (lo, hi) = CALM_CYCLE[self._cycle_index % len(CALM_CYCLE)]
        self._cycle_index += 1
        self._enter_pose(pose, random.uniform(lo, hi))

    def _tick(self):
        dt = 0.033
        self._t += dt
        if self._pose_duration < 1e8:  # 长驻动作（思考/干活，超长时长）由状态信号退出
            self._pose_elapsed += dt
            if self._pose_elapsed >= self._pose_duration:
                self._next_calm_pose()
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        t = self._t

        # 地面阴影
        painter.setPen(Qt.NoPen)
        painter.setBrush(SHADOW)
        painter.drawEllipse(QRectF(40, 100, 88, 9))

        dx = 0.0
        if self._pose == "walk":
            dx = math.sin(t * 2.4) * 9  # 散步时小幅来回踱步
        painter.save()
        painter.translate(dx, 0)

        getattr(self, f"_pose_{self._pose}")(painter, t)
        painter.restore()

    # ----- 通用部件 -----

    @staticmethod
    def _blob(p, cx, cy, rx, ry, fill):
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))

    @staticmethod
    def _capsule(p, x1, y1, x2, y2, width, fill, outline=False):
        if outline:  # 描边层，让独立的小肢体从躯干上分离出来
            edge = QPen(FUR_EDGE, width + 2.2)
            edge.setCapStyle(Qt.RoundCap)
            p.setPen(edge)
            p.drawLine(x1, y1, x2, y2)
        pen = QPen(fill, width)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(x1, y1, x2, y2)

    def _silhouette(self, p, shapes, grow=1.4):
        """躯干两遍绘制：先铺一层略微放大的描边色底，再填本体。

        shapes 元素：("b", cx, cy, rx, ry, fill) 椭圆 / ("c", x1, y1, x2, y2, w, fill) 圆头线。
        fill 可以是 QColor 或 QBrush（渐变）。相邻色块在描边层自然融合成统一剪影。
        """
        p.setPen(Qt.NoPen)
        p.setBrush(FUR_EDGE)
        for s in shapes:
            if s[0] == "b":
                _, cx, cy, rx, ry = s[:5]
                p.drawEllipse(QRectF(cx - rx - grow, cy - ry - grow,
                                     (rx + grow) * 2, (ry + grow) * 2))
            else:
                _, x1, y1, x2, y2, w = s[:6]
                pen = QPen(FUR_EDGE, w + grow * 2)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(x1, y1, x2, y2)
                p.setPen(Qt.NoPen)
        for s in shapes:
            if s[0] == "b":
                _, cx, cy, rx, ry, fill = s
                p.setBrush(fill)
                p.drawEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))
            else:
                _, x1, y1, x2, y2, w, fill = s
                pen = QPen(fill, w)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(x1, y1, x2, y2)
        p.setPen(Qt.NoPen)
        p.setBrush(FUR)

    def _tail(self, p, x0, y0, c1x, c1y, c2x, c2y, x1, y1, w0=8.0, w1=2.4,
              sheen=True):
        """尾巴：三次贝塞尔曲线沿法线两侧偏移成锥形闭合面（根部粗、尾尖细）。

        先铺描边层再填本体；sheen=True 时沿上缘补一道受光细线。
        """
        n = 24
        left, right = [], []
        for i in range(n + 1):
            u = i / n
            mt = 1.0 - u
            x = mt ** 3 * x0 + 3 * mt ** 2 * u * c1x + 3 * mt * u ** 2 * c2x + u ** 3 * x1
            y = mt ** 3 * y0 + 3 * mt ** 2 * u * c1y + 3 * mt * u ** 2 * c2y + u ** 3 * y1
            dx = 3 * mt ** 2 * (c1x - x0) + 6 * mt * u * (c2x - c1x) + 3 * u ** 2 * (x1 - c2x)
            dy = 3 * mt ** 2 * (c1y - y0) + 6 * mt * u * (c2y - c1y) + 3 * u ** 2 * (y1 - c2y)
            ln = math.hypot(dx, dy) or 1.0
            w = (w0 + (w1 - w0) * u) / 2.0
            nx, ny = -dy / ln, dx / ln
            left.append((x + nx * w, y + ny * w))
            right.append((x - nx * w, y - ny * w))
        path = QPainterPath(QPointF(*left[0]))
        for pt in left[1:]:
            path.lineTo(*pt)
        for pt in reversed(right):
            path.lineTo(*pt)
        path.closeSubpath()

        edge = QPen(FUR_EDGE, 1.4)
        edge.setJoinStyle(Qt.RoundJoin)
        p.setPen(edge)
        p.setBrush(FUR_EDGE)
        p.drawPath(path)
        g = QLinearGradient(x0, y0, x1, y1)
        g.setColorAt(0.0, QColor("#332C33"))
        g.setColorAt(1.0, QColor("#1E1A20"))
        p.setBrush(QBrush(g))
        p.drawPath(path)

        if sheen:  # 上缘受光线
            sp = QPainterPath(QPointF(*left[2]))
            for pt in left[2:-4]:
                sp.lineTo(*pt)
            pen = QPen(QColor(84, 74, 80, 90), 1.1)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(sp)
        p.setPen(Qt.NoPen)
        p.setBrush(FUR)

    def _ear(self, p, cx, cy, angle_deg, flip, size=13, edge=False):
        """尖耳（耳缘微弧、耳尖圆钝）。flip=1 近侧耳 / -1 远侧耳；angle 正值顺时针"""
        f = 1.0 if flip >= 0 else -1.0
        path = QPainterPath(QPointF(cx - 5.0 * f, cy + 3.0))
        path.quadTo(cx - 4.0 * f, cy - size * 0.5, cx + 0.8 * f, cy - size)
        path.quadTo(cx + 4.2 * f, cy - size * 0.3, cx + 5.0 * f, cy + 1.8)
        center = QPointF(cx, cy + 2.5)
        tr = QTransform()
        tr.translate(center.x(), center.y())
        tr.rotate(angle_deg)
        tr.translate(-center.x(), -center.y())
        path = tr.map(path)
        if edge:
            pen = QPen(FUR_EDGE, 2.6)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(FUR_EDGE)
        else:
            p.setPen(Qt.NoPen)
            g = QLinearGradient(0, cy - size, 0, cy + 4)
            g.setColorAt(0.0, FUR_HI)
            g.setColorAt(1.0, FUR)
            p.setBrush(QBrush(g))
        p.drawPath(path)
        if not edge:
            inner = QPainterPath(QPointF(cx - 2.8 * f, cy + 1.8))
            inner.quadTo(cx - 2.0 * f, cy - size * 0.38, cx + 0.8 * f, cy - size + 4.0)
            inner.quadTo(cx + 2.4 * f, cy - size * 0.18, cx + 3.0 * f, cy + 1.0)
            p.setBrush(EAR_INNER)
            p.drawPath(tr.map(inner))

    def _cheek_tuft(self, p, cx, cy, fdir, edge=False):
        """腮边炸开的毛须（左右两撮小锯齿），黑猫脸颊的蓬松感"""
        path = QPainterPath(QPointF(cx, cy - 3))
        path.lineTo(cx + 4.5 * fdir, cy - 1)
        path.lineTo(cx + 1.2 * fdir, cy + 0.6)
        path.lineTo(cx + 5.5 * fdir, cy + 3.2)
        path.lineTo(cx + 0.8 * fdir, cy + 5)
        path.lineTo(cx + 3.8 * fdir, cy + 7.5)
        path.lineTo(cx, cy + 8)
        path.closeSubpath()
        if edge:
            pen = QPen(FUR_EDGE, 2.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(FUR_EDGE)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2E272C"))
        p.drawPath(path)

    def _eye(self, p, cx, cy, rx, ry, eye_open, pupil_w=2.6, look=(0.0, 0.0)):
        """照片里黑猫的标志性大圆眼：整颗橙金虹膜 + 又大又圆的黑瞳 + 高光。

        eye_open 为睁眼程度（眨眼时整颗眼压扁），<0.22 时画闭眼线；
        look 控制瞳孔视线偏移；pupil_w 越大瞳孔越圆越大（兴奋/委屈），
        越小越收细（专注）。
        """
        ry_eff = ry * eye_open
        if eye_open < 0.22 or ry_eff < 1.0:  # 闭眼：一条下弯的弧线
            pen = QPen(MOUTH, 1.1)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            lid = QPainterPath(QPointF(cx - rx, cy - ry * 0.1))
            lid.quadTo(QPointF(cx, cy + ry * 0.8), QPointF(cx + rx, cy + ry * 0.1))
            p.drawPath(lid)
            return

        p.save()
        ball = QPainterPath()
        ball.addEllipse(QRectF(cx - rx, cy - ry_eff, rx * 2, ry_eff * 2))
        p.setClipPath(ball)
        px, py = cx + look[0] * rx * 0.25, cy + look[1] * ry_eff * 0.2
        iris = QRadialGradient(QPointF(px, py), rx * 1.4)
        iris.setColorAt(0.0, EYE_GOLD_HI)
        iris.setColorAt(0.5, EYE_GOLD)
        iris.setColorAt(0.85, EYE_GOLD_DEEP)
        iris.setColorAt(1.0, QColor("#6E4A0F"))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(iris))
        p.drawEllipse(QRectF(cx - rx - 1, cy - ry_eff - 1, rx * 2 + 2, ry_eff * 2 + 2))
        # 大圆黑瞳（照片主角）
        pr = min(pupil_w, rx * 0.55)
        self._blob(p, px, py, pr, min(pr * 1.2, ry_eff * 0.62), PUPIL)
        # 高光：主光点 + 次光点
        self._blob(p, cx - rx * 0.42, cy - ry_eff * 0.45, rx * 0.16, rx * 0.16,
                   QColor(255, 255, 255, 235))
        self._blob(p, cx + rx * 0.3, cy + ry_eff * 0.25, rx * 0.1, rx * 0.1,
                   QColor(255, 255, 255, 110))
        p.restore()
        # 深色眼缘（黑猫自带的眼线）
        p.setPen(QPen(QColor("#120E12"), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(cx - rx, cy - ry_eff, rx * 2, ry_eff * 2))

    def _head(self, p, cx, cy, t, eye_open=1.0, pupil_w=2.6, look=(0.0, 0.0),
              ear_l=-10, ear_r=12, sleeping=False, head_tilt=0.0):
        """头部（3/4 视角，面朝左）：圆颅 + 腮边毛须 + 分得较开的双耳。"""
        p.save()
        p.translate(cx, cy)
        if head_tilt:
            p.rotate(head_tilt)

        # 描边层：头 + 双耳 + 腮毛
        p.setPen(Qt.NoPen)
        p.setBrush(FUR_EDGE)
        p.drawEllipse(QRectF(-17.5, -16, 35, 32))
        self._ear(p, -10.5, -10.5, ear_l, -1, size=9.5, edge=True)
        self._ear(p, 10, -10, ear_r, 1, size=9, edge=True)
        self._cheek_tuft(p, -14.5, 4, -1, edge=True)
        self._cheek_tuft(p, 13.5, 4, 1, edge=True)

        # 本体层：径向渐变出球体积（左上受光）
        p.setBrush(_fur_radial(-4, -5, 28))
        p.drawEllipse(QRectF(-16, -14.5, 32, 29))
        self._ear(p, -10.5, -10.5, ear_l, -1, size=9.5)
        self._ear(p, 10, -10, ear_r, 1, size=9)
        self._cheek_tuft(p, -14.5, 4, -1)
        self._cheek_tuft(p, 13.5, 4, 1)

        # 头顶受光弧
        pen = QPen(QColor(90, 80, 84, 110), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        crown = QPainterPath(QPointF(-9, -9.5))
        crown.quadTo(0, -14.5, 8, -10.5)
        p.drawPath(crown)

        # 额头虎斑幽灵纹（黑猫强光下的 M 纹痕迹）
        pen = QPen(QColor(20, 15, 18, 75), 1.3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for fx in (-4.5, 0, 4.5):
            p.drawLine(QPointF(fx, -12.5), QPointF(fx + 0.6, -8.5))

        # 口鼻：浅色块 + 鼻头 + ω 嘴
        self._blob(p, -8, 6.5, 6.5, 4.8, FUR_SHEEN)
        p.setPen(Qt.NoPen)
        p.setBrush(NOSE)
        nose = QPainterPath(QPointF(-11.6, 4.8))
        nose.quadTo(-10.2, 5.9, -10.2, 6.3)
        nose.quadTo(-10.2, 7.3, -11.5, 7.2)
        nose.quadTo(-12.9, 7.3, -12.9, 6.3)
        nose.quadTo(-12.9, 5.9, -11.6, 4.8)
        p.drawPath(nose)
        self._blob(p, -12.4, 5.4, 0.55, 0.45, QColor(255, 220, 225, 70))
        pen = QPen(MOUTH, 1.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        mouth = QPainterPath(QPointF(-11.7, 7.4))     # 人中
        mouth.lineTo(QPointF(-11.5, 8.9))
        mouth.quadTo(QPointF(-13.1, 10.5), QPointF(-14.8, 8.7))   # 左唇
        p.drawPath(mouth)
        mouth2 = QPainterPath(QPointF(-11.5, 8.9))
        mouth2.quadTo(QPointF(-10.0, 10.5), QPointF(-8.2, 8.9))   # 右唇
        p.drawPath(mouth2)
        # 胡须根小点
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(100, 88, 94, 150))
        for dx, dy in ((-13.2, 8.6), (-14.6, 9.6), (-12.6, 10.4)):
            p.drawEllipse(QRectF(dx - 0.45, dy - 0.45, 0.9, 0.9))

        # 胡须（微弯，两侧都有，近侧长远侧短）
        pen = QPen(WHISKER, 0.9)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for sy, dy in ((-1.2, -3.2), (0.6, 0.0), (2.4, 3.0)):
            wq = QPainterPath(QPointF(-12.5, 7.5 + sy))
            wq.quadTo(QPointF(-19, 5.8 + sy), QPointF(-25.5, 4.2 + sy + dy))
            p.drawPath(wq)
        for sy, dy in ((-0.8, -1.6), (1.2, 1.4)):
            wq = QPainterPath(QPointF(2.5, 8.0 + sy))
            wq.quadTo(QPointF(7, 6.8 + sy), QPointF(11.5, 6.0 + sy + dy))
            p.drawPath(wq)

        # 眼睛（近侧大、远侧略窄）
        if sleeping:
            pen = QPen(MOUTH, 1.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            for ex, erx in ((-6.5, 6.0), (5.8, 5.0)):
                lid = QPainterPath(QPointF(ex - erx, -0.4))
                lid.quadTo(ex, 1.8, ex + erx, -0.2)
                p.drawPath(lid)
        else:
            self._eye(p, -6.5, -1.0, 6.2, 5.2, eye_open, pupil_w, look)
            self._eye(p, 5.8, -1.3, 5.2, 4.4, eye_open, pupil_w, look)

        # 腮红（很淡）
        p.setPen(Qt.NoPen)
        p.setBrush(BLUSH)
        p.drawEllipse(QRectF(-13.5, 3.5, 6, 3.2))
        p.drawEllipse(QRectF(6.5, 3.5, 6, 3.2))
        p.restore()

    def _collar(self, p, x, y, tilt=-20):
        """颈带：红项圈 + 金色铃铛"""
        p.save()
        p.translate(x, y)
        p.rotate(tilt)
        p.setPen(Qt.NoPen)
        p.setBrush(COLLAR)
        p.drawRoundedRect(QRectF(-8, -2.5, 16, 5), 2.5, 2.5)
        p.setBrush(COLLAR_DARK)
        p.drawRect(QRectF(-8, 1.2, 16, 1.3))
        p.setBrush(BELL)
        p.drawEllipse(QRectF(-2.6, 2.0, 5.2, 5.2))
        p.setPen(QPen(QColor(120, 84, 20), 0.8))
        p.drawLine(QPointF(-2.4, 4.6), QPointF(2.4, 4.6))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(-0.4, 3.9, 0.8, 0.8))
        p.restore()

    def _blink_openness(self, t, period=3.7):
        """周期性眨眼：返回 0..1 睁眼程度"""
        phase = (t + self._blink_seed) % period
        if phase < 0.13:
            return abs(phase / 0.065 - 1.0) * 0.9 + 0.1
        return 1.0

    def _sitting_body(self, p, t):
        """端坐躯干（不含头）：一条完整轮廓画出 背线→圆臀→胸线，渐变填充无接缝"""
        swish = math.sin(t * 2.8)
        self._tail(p, 114, 84, 128, 100, 90, 105, 50 + swish * 5, 97 - abs(swish) * 2)

        body = QPainterPath(QPointF(58, 43))
        body.quadTo(82, 45, 97, 58)          # 颈后沿背线到臀顶
        body.quadTo(121, 68, 119, 82)        # 臀部外弧
        body.quadTo(118, 97, 97, 97)         # 臀底到地面
        body.lineTo(61, 97)                  # 底边
        body.quadTo(56, 78, 57, 62)          # 胸前线
        body.quadTo(57.5, 50, 58, 43)
        body.closeSubpath()
        edge = QPen(FUR_EDGE, 3.0)
        edge.setJoinStyle(Qt.RoundJoin)
        p.setPen(edge)
        p.setBrush(FUR_EDGE)
        p.drawPath(body)
        p.setPen(Qt.NoPen)
        p.setBrush(_fur_radial(80, 60, 46))
        p.drawPath(body)

        # 后脚（露在臀侧）与前腿：远侧腿贴在近侧腿后只露一条边，避免读成身体上的洞
        self._blob(p, 108, 96, 6.5, 3.5, FUR_DEEP)
        self._capsule(p, 73, 68, 73, 94, 7, QColor("#221D2E"))
        self._blob(p, 73.5, 95, 5.2, 3.4, QColor("#241F30"))
        self._capsule(p, 65, 66, 65, 95, 7.5, _fur_linear(66, 96))
        self._blob(p, 64.5, 96, 5.8, 3.6, FUR_SHEEN)
        pen = QPen(QColor(15, 11, 20, 120), 0.8)                # 脚趾缝
        p.setPen(pen)
        p.drawLine(QPointF(62.5, 95.5), QPointF(62.5, 97.5))
        p.drawLine(QPointF(66.5, 95.5), QPointF(66.5, 97.5))
        # 胸口受光
        p.setPen(Qt.NoPen)
        self._blob(p, 62, 52, 7, 5, QColor(90, 80, 84, 70))

    def _tail_wrapped(self, p, t, amp=5.0, freq=2.8):
        """端坐时尾巴贴地绕到前爪旁，尾尖轻摇"""
        swish = math.sin(t * freq)
        self._tail(p, 114, 84, 128, 100, 90, 105,
                   50 + swish * amp, 97 - abs(swish) * 2)

    # ----- 各姿态 -----

    def _pose_sit(self, p, t):
        bob = math.sin(t * 1.3) * 1.0
        tw = math.sin(t * 0.9) * 3  # 耳朵偶尔抖动

        self._sitting_body(p, t)
        self._collar(p, 59, 52 + bob * 0.6)
        self._head(p, 52, 38 + bob, t,
                   eye_open=self._blink_openness(t),
                   ear_l=-10 + tw, ear_r=12 - tw)

    def _pose_walk(self, p, t):
        speed, swing, bob_amp = 4.2, 0.5, 1.2
        bob = math.sin(t * speed) * bob_amp
        wave = math.sin(t * 8) * 5

        # 尾巴向后拉成流线
        self._tail(p, 108, 60 + bob, 124, 50 + bob, 136, 58 + wave, 144, 48 + wave,
                   6.5, 2.0)

        # 四足交替步态：前腿向前伸、后腿向后蹬
        def leg(sx, sy, phase, fill, rear=False, L=24, width=7):
            a = math.sin(t * speed + phase) * swing
            tx = sx + (1 if rear else -1) * math.sin(a) * L
            ty = sy + math.cos(a) * L
            return ("c", sx, sy, tx, ty, width, fill), (tx, ty)

        far_f, f1t = leg(72, 66 + bob, 0, FUR_DEEP)
        far_r, f2t = leg(102, 66 + bob, math.pi, FUR_DEEP, rear=True)
        near_f, n1t = leg(66, 69 + bob, 0.35, _fur_linear(66, 94))
        near_r, n2t = leg(108, 69 + bob, math.pi + 0.35, _fur_linear(66, 94), rear=True)
        self._silhouette(p, [
            far_f, far_r,
            ("b", 86, 63 + bob, 30, 12, _fur_radial(80, 56, 38)),    # 躯干一体
            ("b", 56, 56 + bob, 9, 9, FUR),                          # 颈
            near_f, near_r,
        ])
        self._blob(p, 78, 70 + bob, 16, 4.5, QColor(80, 72, 76, 60))  # 腹部反光
        for tx, ty in (f1t, f2t, n1t, n2t):
            self._blob(p, tx, ty + 0.5, 3.8, 2.8, FUR_SHEEN)

        self._collar(p, 56, 59 + bob, tilt=-30)
        self._head(p, 46, 47 + bob * 0.7, t, eye_open=0.9, pupil_w=1.5,
                   look=(-0.5, 0.1), ear_l=24 + math.sin(t * speed) * 3,
                   ear_r=28 + math.sin(t * speed) * 3)

    def _pose_drowsy(self, p, t):
        # 头部缓慢下垂，周期末快速抬起
        cycle = 5.0
        ph = (t % cycle) / cycle
        if ph < 0.72:
            droop = (ph / 0.72) ** 1.8 * 18
        else:
            droop = 18 * (1 - (ph - 0.72) / 0.28)
        eye_open = max(1.0 - droop / 18 * 0.85, 0.1)

        self._sitting_body(p, t)
        self._tail_wrapped(p, t, amp=3.0, freq=1.6)
        self._collar(p, 59, 52 + droop * 0.4)
        self._head(p, 52, 38 + droop, t, eye_open=eye_open, pupil_w=2.2,
                   look=(0.0, 0.6), ear_l=-16 - droop * 0.3, ear_r=18 + droop * 0.3,
                   head_tilt=droop * 0.8)

        # 快睡着时冒一个小 z
        if droop > 12:
            alpha = int(160 * (droop - 12) / 6)
            self._draw_z(p, 76, 24, 9, alpha)

    def _pose_sleep(self, p, t):
        breathe = math.sin(t * 1.5)

        # 尾巴贴地包住身前（画在躯干下层）
        self._tail(p, 108, 86, 122, 100, 76, 104, 46, 90 - breathe, 7, 2.4)
        # 蜷成一团的身体（呼吸起伏）
        self._silhouette(p, [
            ("b", 88, 83, 29 + breathe * 1.1, 13.5 - breathe * 0.7,
             _fur_radial(82, 76, 34)),
            ("b", 106, 79, 15, 11, _fur_radial(102, 74, 20)),    # 后臀隆起
        ])
        # 缩在脸颊下的前爪
        self._blob(p, 52, 89, 5, 3.5, FUR_SHEEN)
        self._blob(p, 60, 91, 5, 3.5, FUR_SHEEN)

        self._head(p, 57, 74, t, sleeping=True, ear_l=-14, ear_r=16, head_tilt=9)

        # Zzz 气泡
        for i in range(3):
            ph = ((t * 0.32) + i / 3.0) % 1.0
            x = 74 + ph * 30 + i * 2
            y = 44 - ph * 24
            size = 8 + ph * 8
            alpha = int(210 * (1 - ph))
            self._draw_z(p, x, y, size, alpha)

    def _pose_stretch(self, p, t):
        wag = math.sin(t * 7) * 9

        # 尾巴高高翘成问号形
        self._tail(p, 110, 50, 124, 36, 138, 44 + wag * 0.4, 132, 56 + wag * 0.6,
                   6.5, 2.2)
        # 远侧前腿前伸贴地（画在躯干后）
        self._capsule(p, 56, 82, 36, 96, 6.5, FUR_DEEP)
        self._blob(p, 35, 97, 4, 3, QColor("#241F30"))

        # 躯干一体轮廓：背线从颈后升到抬起的臀顶，腹线斜向前下
        body = QPainterPath(QPointF(48, 68))
        body.quadTo(72, 56, 92, 50)
        body.quadTo(116, 48, 118, 64)
        body.quadTo(118, 78, 104, 80)
        body.quadTo(80, 84, 62, 84)
        body.quadTo(50, 80, 48, 68)
        body.closeSubpath()
        edge = QPen(FUR_EDGE, 3.0)
        edge.setJoinStyle(Qt.RoundJoin)
        p.setPen(edge)
        p.setBrush(FUR_EDGE)
        p.drawPath(body)
        p.setPen(Qt.NoPen)
        p.setBrush(_fur_radial(84, 60, 44))
        p.drawPath(body)

        # 后腿从抬起的臀部撑直到地面（远侧暗、近侧亮）
        self._capsule(p, 99, 74, 102, 95, 7, QColor("#221D2E"))
        self._blob(p, 102.5, 96, 4.2, 3.2, QColor("#241F30"))
        self._capsule(p, 107, 72, 110, 95, 7, _fur_linear(70, 96))
        self._blob(p, 110.5, 96, 4.2, 3.2, FUR_SHEEN)
        # 近侧前腿前伸
        self._capsule(p, 52, 84, 32, 97, 6.5, _fur_linear(82, 98))
        self._blob(p, 31.5, 98, 4, 3, FUR_SHEEN)

        self._collar(p, 45, 71, tilt=-55)
        self._head(p, 38, 64, t, eye_open=1.0, pupil_w=1.8, look=(-0.3, 0.5),
                   ear_l=-8, ear_r=6, head_tilt=-12)

    def _pose_think(self, p, t):
        """思考：抬爪抵腮、轻微歪头、瞳孔望向上方、头顶漂浮问号"""
        bob = math.sin(t * 1.2) * 1.0
        tilt = math.sin(t * 0.7) * 4.0

        self._sitting_body(p, t)
        self._tail_wrapped(p, t, amp=2.0, freq=1.8)
        # 近侧前爪抬起抵腮
        paw_x = 50 + math.sin(t * 1.1) * 1.0
        self._capsule(p, 64, 68, paw_x + 4, 52, 6.5, FUR, outline=True)
        self._blob(p, paw_x, 50, 4.5, 3.2, FUR_SHEEN)

        self._collar(p, 59, 52 + bob)
        self._head(p, 52, 36 + bob, t,
                   eye_open=self._blink_openness(t), pupil_w=2.6,
                   look=(0.15, -0.9), ear_l=-8 + tilt, ear_r=10 + tilt * 0.5,
                   head_tilt=-7 + tilt)
        # 头顶漂浮的问号
        qa = int(150 + 60 * math.sin(t * 2.0))
        self._draw_question(p, 76, 16 + math.sin(t * 1.5) * 2.5, 9, qa)

    def _pose_work(self, p, t):
        """干活：前低后高的鞠躬姿势，前爪快速交替刨地，尘土飞扬"""
        speed = 12.0
        bob = math.sin(t * speed) * 0.7
        wag = math.sin(t * 9) * 11

        # 尾巴上翘快速摆动
        self._tail(p, 106, 46 + bob, 118, 36 + bob, 130, 40 + wag * 0.5, 126, 50 + wag,
                   6.5, 2.2)

        # 躯干一体轮廓：臀高头低的弓背
        body = QPainterPath(QPointF(58, 50 + bob))
        body.quadTo(78, 40 + bob, 96, 40 + bob)     # 背线升到抬起的臀顶
        body.quadTo(120, 40 + bob, 118, 56 + bob)
        body.quadTo(117, 72 + bob, 104, 78 + bob)
        body.quadTo(84, 74 + bob, 66, 66 + bob)     # 腹线回到前胸
        body.quadTo(58, 60 + bob, 58, 50 + bob)
        body.closeSubpath()
        edge = QPen(FUR_EDGE, 3.0)
        edge.setJoinStyle(Qt.RoundJoin)
        p.setPen(edge)
        p.setBrush(FUR_EDGE)
        p.drawPath(body)
        p.setPen(Qt.NoPen)
        p.setBrush(_fur_radial(86, 50 + bob, 44))
        p.drawPath(body)

        # 后腿蹬地（远侧暗、近侧亮）
        self._capsule(p, 100, 60 + bob, 108, 82, 7, QColor("#221D2E"))
        self._blob(p, 108.5, 83, 4.2, 3.2, QColor("#241F30"))
        self._capsule(p, 110, 58 + bob, 118, 80, 7, _fur_linear(56, 82))
        self._blob(p, 118.5, 81, 4.2, 3.2, FUR_SHEEN)

        # 前爪交替快速刨地（伸到头前方的地面）
        def paw(phase, fill):
            a = math.sin(t * speed + phase)
            x = 36 + a * 7
            y = 87 - abs(a) * 6
            self._capsule(p, 66, 64 + bob, x, y, 6.5, fill, outline=True)
            self._blob(p, x, y + 0.5, 3.6, 2.8, FUR_SHEEN)

        paw(math.pi, QColor("#221D2E"))
        paw(0.0, FUR)

        self._collar(p, 52, 58 + bob, tilt=-30)
        self._head(p, 46, 46 + bob, t, eye_open=0.95, pupil_w=1.5,
                   look=(-0.4, 0.4), ear_l=-4, ear_r=4, head_tilt=-6)

        # 扒出的尘土向前上扬
        for i in range(3):
            ph = (t * 1.6 + i / 3.0) % 1.0
            dust = QColor(180, 168, 150, int(150 * (1 - ph)))
            self._blob(p, 22 + ph * 10 + i * 3, 84 - ph * 14 - i * 2,
                       2.5 + ph * 3, 2 + ph * 2.5, dust)

    def _pose_happy(self, p, t):
        """开心：原地跳跃、前爪举起、尾巴大幅甩动、星星闪烁、瞳孔放大"""
        hop = abs(math.sin(t * 4.2)) * 11.0
        yb = -hop
        wag = math.sin(t * 13) * 18.0

        # 影子随高度缩小
        p.setPen(Qt.NoPen)
        p.setBrush(SHADOW)
        sw = 1.0 - hop / 28.0
        p.drawEllipse(QRectF(84 - 38 * sw, 102, 76 * sw, 8))

        # 闪烁的小星星
        self._draw_star(p, 24, 40 + math.sin(t * 3.1) * 3, 6, 140 + 90 * math.sin(t * 5))
        self._draw_star(p, 136, 30 + math.cos(t * 2.6) * 3, 5, 140 + 90 * math.sin(t * 5 + 2))

        # 尾巴上扬大幅甩动
        self._tail(p, 104, 56 + yb, 118, 40 + yb, 132, 46 + wag, 128, 58 + wag,
                   6.5, 2.2)
        self._silhouette(p, [
            ("c", 96, 72 + yb, 106, 88 + yb, 7, FUR_DEEP),       # 后腿后蹬
            ("c", 100, 70 + yb, 112, 84 + yb, 7, _fur_linear(66, 90)),
            ("b", 78, 64 + yb, 24, 11.5, _fur_linear(50 + yb, 78 + yb)),
            ("b", 56, 55 + yb, 8, 8, FUR),
            ("c", 68, 62 + yb, 58, 46 + yb, 6.5, FUR_DEEP),      # 前爪高举
            ("c", 62, 60 + yb, 50, 44 + yb, 6.5, _fur_linear(60, 42)),
        ])
        self._blob(p, 58, 44 + yb, 4, 3, FUR_SHEEN)
        self._blob(p, 50, 42 + yb, 4, 3, FUR_SHEEN)

        self._collar(p, 54, 58 + yb, tilt=-34)
        self._head(p, 46, 42 + yb, t, eye_open=1.0, pupil_w=3.0,
                   look=(-0.2, -0.2), ear_l=16, ear_r=20, head_tilt=-6)

    def _pose_sad(self, p, t):
        """失落：飞机耳低头、尾巴拖地、瞳孔放大、头顶冒汗"""
        droop = 4 + math.sin(t * 1.0)
        wag = math.sin(t * 1.3) * 2.5

        # 尾巴无精打采地拖在地上
        self._tail(p, 114, 92, 126, 97, 134, 95 + wag, 142, 92 + wag * 0.6,
                   6.5, 2.0, sheen=False)
        body = QPainterPath(QPointF(56, 60 + droop))
        body.quadTo(80, 63, 96, 74)          # 耷拉的背线
        body.quadTo(120, 84, 118, 95)
        body.quadTo(112, 99, 90, 98)
        body.lineTo(66, 98)
        body.quadTo(59, 80, 56, 60 + droop)
        body.closeSubpath()
        edge = QPen(FUR_EDGE, 3.0)
        edge.setJoinStyle(Qt.RoundJoin)
        p.setPen(edge)
        p.setBrush(FUR_EDGE)
        p.drawPath(body)
        p.setPen(Qt.NoPen)
        p.setBrush(_fur_radial(82, 76, 44))
        p.drawPath(body)

        self._capsule(p, 76, 80, 76, 96, 7, QColor("#221D2E"))  # 远侧前腿
        self._blob(p, 76.5, 97, 5.2, 3.4, QColor("#241F30"))
        self._capsule(p, 68, 78, 68, 97, 7.5, _fur_linear(76, 98))
        self._blob(p, 67.5, 98, 5.8, 3.6, FUR_SHEEN)
        self._blob(p, 61, 64 + droop, 8.5, 8, FUR)              # 颈
        self._blob(p, 72, 86, 8, 8, QColor(90, 80, 84, 55))    # 胸口受光

        self._collar(p, 60, 66 + droop, tilt=-10)
        self._head(p, 54, 48 + droop, t, eye_open=0.42, pupil_w=3.2,
                   look=(0.0, 0.4), ear_l=-55, ear_r=55, head_tilt=9)  # 飞机耳
        # 头顶的汗滴
        self._draw_sweat(p, 74, 26 + math.sin(t * 2.0), 6, 170)

    @staticmethod
    def _draw_z(p, x, y, size, alpha):
        """用线段画 Z（避免环境缺字体）"""
        if alpha <= 0:
            return
        pen = QPen(QColor(116, 128, 158, min(int(alpha), 255)), max(size / 7.0, 1.3))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        h = size / 2.0
        p.drawLine(QPointF(x - h, y - h), QPointF(x + h, y - h))
        p.drawLine(QPointF(x + h, y - h), QPointF(x - h, y + h))
        p.drawLine(QPointF(x - h, y + h), QPointF(x + h, y + h))

    @staticmethod
    def _draw_question(p, x, y, size, alpha):
        """用弧线画漂浮的 ？（避免环境缺字体）"""
        if alpha <= 0:
            return
        col = QColor(INK.red(), INK.green(), INK.blue(), min(int(alpha), 255))
        pen = QPen(col, max(size / 6.0, 1.4))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # 问号：上半段弧 + 竖线 + 点
        p.drawArc(QRectF(x - size * 0.45, y - size * 0.62, size * 0.9, size * 0.95),
                  40 * 16, 215 * 16)
        p.drawLine(QPointF(x, y + size * 0.02), QPointF(x, y + size * 0.34))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QRectF(x - size * 0.1, y + size * 0.52, size * 0.2, size * 0.2))

    @staticmethod
    def _draw_star(p, x, y, size, alpha):
        """用线段画四角星星（撒花/庆祝）"""
        if alpha <= 0:
            return
        col = QColor(SPARK.red(), SPARK.green(), SPARK.blue(), min(int(alpha), 255))
        pen = QPen(col, max(size / 4.0, 1.2))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(x - size, y), QPointF(x + size, y))
        p.drawLine(QPointF(x, y - size), QPointF(x, y + size))

    @staticmethod
    def _draw_sweat(p, x, y, size, alpha):
        """画一滴汗（出状况时）"""
        if alpha <= 0:
            return
        col = QColor(SWEAT.red(), SWEAT.green(), SWEAT.blue(), min(int(alpha), 255))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QRectF(x - size * 0.55, y - size * 0.1, size * 1.1, size * 1.1))
        p.drawEllipse(QRectF(x - size * 0.22, y - size * 0.75, size * 0.44, size * 0.6))

    # ---------- 交互 ----------

    def hideEvent(self, event):
        # 隐藏时暂停 30fps 动画时钟，避免不可见时持续空转重绘
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start(33)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_active = True
            self._drag_moved = False
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag_active:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if (new_pos - self.pos()).manhattanLength() > 3:
                self._drag_moved = True
            self.move(new_pos)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_active:
            self._drag_active = False
            if not self._drag_moved:
                self.hide()
                if hasattr(self, "_main_window"):
                    self._main_window.showNormal()
                    self._main_window.raise_()
                    self._main_window.activateWindow()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: white; border: 1px solid #e2e8f0;
                border-radius: 8px; padding: 4px;
            }
            QMenu::item { padding: 8px 20px; border-radius: 4px; color: #2d3748; }
            QMenu::item:selected { background: #edf2f7; }
        """)
        stretch_action = menu.addAction("🐈 伸个懒腰")
        menu.addSeparator()
        exit_action = menu.addAction("🐈‍⬛ 退出程序")
        action = menu.exec(event.globalPos())
        if action == stretch_action:
            self._enter_pose("stretch", 2.2)
        elif action == exit_action:
            if hasattr(self, "_main_window") and hasattr(self._main_window, "request_real_exit"):
                self._main_window.request_real_exit()
            else:
                QApplication.instance().quit()

    def show_at_bottom_right(self):
        """显示在屏幕右下角"""
        screens = QApplication.instance().screens()
        if screens:
            geo = screens[0].geometry()
            self.move(geo.right() - self.width() - 20,
                      geo.bottom() - self.height() - 60)
        self.show()
        self.raise_()
