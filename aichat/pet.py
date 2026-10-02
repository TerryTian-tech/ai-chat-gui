"""桌宠组件：一只会做多种自然动作的可爱小狗。

姿态：坐着摇尾巴 / 散步 / 奔跑 / 犯困点头 / 趴睡（带 Zzz）/ 伸懒腰，
以及与 AI 任务联动的四种姿态：思考（托腮冒问号）/ 干活（刨地）/ 开心（跳跃撒花）/ 失落（垂耳冒汗）。
平时按随机时长的状态机自然轮换；AI 思考、执行工具或检测到新进程时切换到对应动作。
全部图形用 QPainter 参数化绘制，动画由单一计时器驱动。
"""

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# 配色：金毛/柴犬风
FUR = QColor("#E8A85D")
FUR_DARK = QColor("#C9803F")     # 耳朵、远侧腿
CREAM = QColor("#FBEDD7")        # 口鼻、爪子、肚皮
EYE = QColor("#3B2F2A")
BLUSH = QColor(244, 143, 143, 115)
TONGUE = QColor("#F58E8E")
COLLAR = QColor("#E05B5B")
TAG = QColor("#F5C242")
SHADOW = QColor(60, 50, 40, 28)
INK = QColor(116, 128, 158)      # ？/Z 等气泡线条色
SPARK = QColor(245, 194, 66)     # 星星
SWEAT = QColor(91, 155, 213)     # 汗滴

POSE_LABELS = {
    "sit": "坐着摇尾巴 🐶",
    "walk": "散步中 🐾",
    "run": "奔跑中 🐕💨",
    "drowsy": "犯困中 🥱",
    "sleep": "趴睡中 😴",
    "stretch": "伸懒腰~ 🐕",
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


class PuppyWidget(QWidget):
    """桌面小狗组件：点击恢复主窗口，可拖动，右键菜单退出"""

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
        self._run_flags = {}         # 外部奔跑触发源（process 等）
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
        - idle：回到平静轮换（若进程监控仍触发奔跑则继续奔跑）
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
            if any(self._run_flags.values()):
                self._enter_pose("run", 1e9)
            else:
                self._enter_pose("stretch", 1.6)

    def set_run_flag(self, key: str, on: bool):
        """外部奔跑触发源（检测到新进程等）；AI 任务动作优先，忙碌时忽略"""
        was_running = any(self._run_flags.values())
        self._run_flags[key] = on
        is_running = any(self._run_flags.values())
        if self._ai_state in ("thinking", "working", "happy", "sad"):
            return
        if on and not was_running:
            self._enter_pose("run", 1e9)
        elif not on and was_running and not is_running:
            self._enter_pose("stretch", 1.6)
            self._cycle_index = 0

    # ---------- 状态机 ----------

    def _enter_pose(self, pose: str, duration: float):
        self._pose = pose
        self._pose_elapsed = 0.0
        self._pose_duration = duration
        self.setToolTip(f"小狗：{POSE_LABELS.get(pose, pose)}")

    def _next_calm_pose(self):
        pose, (lo, hi) = CALM_CYCLE[self._cycle_index % len(CALM_CYCLE)]
        self._cycle_index += 1
        self._enter_pose(pose, random.uniform(lo, hi))

    def _tick(self):
        dt = 0.033
        self._t += dt
        if self._pose_duration < 1e8:  # 外部触发的奔跑(超长时长)由信号退出
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
        painter.drawEllipse(QRectF(40, 100, 90, 10))

        dx = 0.0
        if self._pose == "walk":
            dx = math.sin(t * 2.4) * 9  # 散步时小幅来回踱步
        painter.save()
        painter.translate(dx, 0)

        getattr(self, f"_pose_{self._pose}")(painter, t)
        painter.restore()

    # ----- 通用部件 -----

    @staticmethod
    def _capsule(p, x1, y1, x2, y2, width, color):
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(x1, y1, x2, y2)

    @staticmethod
    def _blob(p, cx, cy, rx, ry, color):
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QRectF(cx - rx, cy - ry, rx * 2, ry * 2))

    def _ear(self, p, base_x, base_y, angle_deg, length=23):
        """垂耳：以头顶侧为轴的旋转椭圆，画在头部之上"""
        p.save()
        p.translate(base_x, base_y)
        p.rotate(angle_deg)
        p.setPen(Qt.NoPen)
        p.setBrush(FUR_DARK)
        p.drawEllipse(QRectF(-4.5, -3, 9, length))
        p.restore()

    def _head(self, p, cx, cy, t, eye_open=1.0, ear_left=17, ear_right=-17,
              tongue=False, sleeping=False, head_tilt=0.0):
        """头部（3/4 视角，面朝左）。ear_*: 两耳旋转角，正值向外垂、负值向后飘。"""
        p.save()
        p.translate(cx, cy)
        if head_tilt:
            p.rotate(head_tilt)

        # 头
        self._blob(p, 0, 0, 20, 18.5, FUR)
        # 头顶浅色斑
        self._blob(p, -2, -9, 11, 6.5, CREAM)
        # 垂耳从头顶两侧垂下（覆盖在头上，保证可见）
        self._ear(p, -9, -14, ear_left)
        self._ear(p, 9, -14, ear_right)

        # 口鼻
        self._blob(p, -5, 8, 10.5, 7.5, CREAM)
        # 鼻子
        self._blob(p, -9, 4.5, 3, 2.4, EYE)

        # 嘴巴 ω（下弧 = 微笑）
        pen = QPen(QColor(90, 66, 56), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(-10, 6, 5, 3.5), 180 * 16, 180 * 16)
        p.drawArc(QRectF(-5, 6, 5, 3.5), 180 * 16, 180 * 16)

        if tongue:
            self._blob(p, -8, 13.5, 3, 4.5, TONGUE)

        # 眼睛（大眼 + 双高光）
        for ex in (-10, 4):
            if sleeping or eye_open <= 0.15:
                p.setPen(QPen(EYE, 1.8))
                p.drawArc(QRectF(ex - 3.5, -2, 7, 4), 180 * 16, 180 * 16)
            else:
                ry = max(3.6 * eye_open, 0.7)
                self._blob(p, ex, -1, 3.6, ry, EYE)
                if eye_open > 0.55:
                    self._blob(p, ex - 1.2, -1.4 - ry * 0.4, 1.4, 1.4, QColor(255, 255, 255))
                    self._blob(p, ex + 1.3, -0.6 - ry * 0.2, 0.8, 0.8, QColor(255, 255, 255))

        # 腮红
        p.setPen(Qt.NoPen)
        p.setBrush(BLUSH)
        p.drawEllipse(QRectF(-16, 4, 7, 4))
        p.drawEllipse(QRectF(7, 4, 7, 4))
        p.restore()

    def _collar(self, p, x, y, tilt=-32):
        """颈带：斜跨颈部的短粗条 + 金色吊牌"""
        p.save()
        p.translate(x, y)
        p.rotate(tilt)
        p.setPen(Qt.NoPen)
        p.setBrush(COLLAR)
        p.drawRoundedRect(QRectF(-7, -3, 14, 6), 3, 3)
        p.setBrush(TAG)
        p.drawEllipse(QRectF(-3, 2.5, 6, 6))
        p.restore()

    def _blink_openness(self, t, period=3.7):
        """周期性眨眼：返回 0..1 睁眼程度"""
        phase = (t + self._blink_seed) % period
        if phase < 0.13:
            return abs(phase / 0.065 - 1.0) * 0.9 + 0.1
        return 1.0

    def _tail(self, p, base_x, base_y, angle_deg, length=18, width=6):
        """尾巴：从基部按角度甩出的圆头粗线"""
        rad = math.radians(angle_deg)
        tip_x = base_x + math.cos(rad) * length
        tip_y = base_y - math.sin(rad) * length
        pen = QPen(FUR, width)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(base_x, base_y, tip_x, tip_y)
        # 尾尖浅色
        self._blob(p, tip_x, tip_y, width / 2 - 0.5, width / 2 - 0.5, CREAM)

    # ----- 各姿态 -----

    def _pose_sit(self, p, t):
        wag = math.sin(t * 6.5) * 22
        bob = math.sin(t * 1.3) * 1.2

        self._tail(p, 120, 70, 40 + wag)
        # 后半身
        self._blob(p, 100, 80, 25, 19, FUR)
        # 后爪
        self._blob(p, 108, 94, 8, 5.5, CREAM)
        # 远侧前腿
        self._capsule(p, 80, 70, 80, 94, 9, FUR_DARK)
        self._blob(p, 80, 95, 5.5, 4, CREAM)
        # 胸前
        self._blob(p, 76, 72, 16, 24, FUR)
        self._blob(p, 72, 80, 9, 11, CREAM)
        # 近侧前腿
        self._capsule(p, 68, 70, 68, 95, 9, FUR)
        self._blob(p, 68, 96, 5.5, 4, CREAM)
        # 颈部
        p.save()
        p.translate(62, 56 + bob)
        p.rotate(35)
        self._blob(p, 0, 0, 9, 11, FUR)
        p.restore()

        self._collar(p, 63, 61 + bob)
        self._head(p, 54, 40 + bob, t,
                   eye_open=self._blink_openness(t),
                   ear_left=17 + math.sin(t * 0.9) * 4,
                   ear_right=-17 - math.sin(t * 0.9) * 3)

    def _pose_walk(self, p, t):
        self._pose_run(p, t, speed=4.5, swing=0.45, bob_amp=1.4, tongue=False)

    def _pose_run(self, p, t, speed=11.0, swing=0.78, bob_amp=2.4, tongue=True):
        bob = math.sin(t * speed) * bob_amp
        body_cx, body_cy = 84, 60 + bob

        # 速度线
        pen = QPen(QColor(160, 160, 170, 110), 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for y in (52, 62):
            p.drawLine(122, y + bob, 138, y + bob)

        self._tail(p, 112, 54 + bob, 35 + math.sin(t * 9) * 12, 20, 6)

        def leg(px, py, phase, color, width=9.5):
            a = math.sin(t * speed + phase) * swing
            tx = px - math.sin(a) * 19
            ty = py + math.cos(a) * 19
            self._capsule(p, px, py, tx, ty, width, color)
            self._blob(p, tx, ty + 1, 4.2, 3.4, CREAM)

        # 远侧腿（对角步态）
        leg(96, 66 + bob, 0, FUR_DARK)
        leg(70, 66 + bob, math.pi, FUR_DARK)
        # 身体
        self._blob(p, body_cx, body_cy, 29, 13.5, FUR)
        self._blob(p, body_cx - 4, body_cy + 6, 20, 6, CREAM)  # 肚皮
        # 近侧腿
        leg(92, 68 + bob, math.pi, FUR)
        leg(66, 68 + bob, 0, FUR)

        # 颈部连接头与身体
        p.save()
        p.translate(54, 52 + bob)
        p.rotate(30)
        self._blob(p, 0, 0, 8, 9, FUR)
        p.restore()

        self._collar(p, 55, 56 + bob, tilt=-28)
        self._head(p, 44, 46 + bob * 0.8, t, eye_open=1.0,
                   ear_left=-34 + math.sin(t * speed) * 5,
                   ear_right=-52 + math.sin(t * speed) * 5, tongue=tongue)

    def _pose_drowsy(self, p, t):
        # 头部缓慢下垂，周期末快速抬起
        cycle = 5.0
        ph = (t % cycle) / cycle
        if ph < 0.72:
            droop = (ph / 0.72) ** 1.8 * 20
        else:
            droop = 20 * (1 - (ph - 0.72) / 0.28)
        eye_open = max(1.0 - droop / 20 * 0.8, 0.12)
        wag = math.sin(t * 2.2) * 8  # 困了尾巴摇得慢

        self._tail(p, 120, 70, 38 + wag)
        self._blob(p, 100, 80, 25, 19, FUR)
        self._blob(p, 108, 94, 8, 5.5, CREAM)
        self._capsule(p, 80, 70, 80, 94, 9, FUR_DARK)
        self._blob(p, 80, 95, 5.5, 4, CREAM)
        self._blob(p, 76, 72, 16, 24, FUR)
        self._capsule(p, 68, 70, 68, 95, 9, FUR)
        self._blob(p, 68, 96, 5.5, 4, CREAM)

        p.save()
        p.translate(62, 56 + droop * 0.4)
        p.rotate(35 + droop * 0.5)
        self._blob(p, 0, 0, 9, 11, FUR)
        p.restore()

        self._collar(p, 63, 61 + droop * 0.4)
        self._head(p, 54, 40 + droop, t, eye_open=eye_open,
                   ear_left=26 + droop * 0.4, ear_right=-24 - droop * 0.3,
                   head_tilt=droop * 0.8)

        # 快睡着时冒一个小 z
        if droop > 14:
            alpha = int(160 * (droop - 14) / 6)
            self._draw_z(p, 78, 26, 9, alpha)

    def _pose_sleep(self, p, t):
        breathe = math.sin(t * 1.6)

        # 蜷起的尾巴（绕到身前）
        pen = QPen(FUR, 7)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(QRectF(96, 74, 34, 26), 0, -100 * 16)

        # 身体（呼吸起伏）
        self._blob(p, 86, 82, 33 + breathe * 1.2, 12.5 - breathe * 0.8, FUR)
        self._blob(p, 88, 87, 22 - breathe * 0.5, 5, CREAM)
        # 前爪垫在头下
        self._blob(p, 52, 92, 6, 4, CREAM)
        self._blob(p, 62, 93, 6, 4, CREAM)

        self._head(p, 54, 74, t, sleeping=True, ear_left=30, ear_right=-16, head_tilt=6)

        # Zzz 气泡
        for i in range(3):
            ph = ((t * 0.32) + i / 3.0) % 1.0
            x = 76 + ph * 30 + i * 2
            y = 46 - ph * 26
            size = 8 + ph * 8
            alpha = int(210 * (1 - ph))
            self._draw_z(p, x, y, size, alpha)

    def _pose_stretch(self, p, t):
        wag = math.sin(t * 8) * 10

        self._tail(p, 122, 58, 62 + wag, 17)
        # 抬起的后半身
        self._blob(p, 100, 62, 24, 17, FUR)
        self._capsule(p, 106, 72, 108, 96, 8, FUR_DARK)
        self._blob(p, 108, 97, 4.5, 3.5, CREAM)
        self._capsule(p, 114, 70, 118, 96, 8, FUR)
        self._blob(p, 118, 97, 4.5, 3.5, CREAM)
        # 前倾趴低
        self._blob(p, 64, 79, 20, 12, FUR)
        self._capsule(p, 62, 80, 34, 93, 8, FUR_DARK)
        self._blob(p, 33, 94, 4.5, 3.5, CREAM)
        self._capsule(p, 56, 82, 22, 98, 8, FUR)
        self._blob(p, 21, 99, 4.5, 3.5, CREAM)

        self._collar(p, 48, 68, tilt=-18)
        self._head(p, 44, 59, t, eye_open=1.0, ear_left=34, ear_right=-30, head_tilt=-8)

    def _pose_think(self, p, t):
        """思考：托腮、轻微歪头、头顶漂浮问号、尾巴慢摇"""
        bob = math.sin(t * 1.2) * 1.0
        tilt = math.sin(t * 0.7) * 4.0
        wag = math.sin(t * 2.5) * 6.0

        self._tail(p, 120, 70, 42 + wag)
        # 后半身与后爪
        self._blob(p, 100, 80, 25, 19, FUR)
        self._blob(p, 108, 94, 8, 5.5, CREAM)
        # 远侧前腿（撑地）
        self._capsule(p, 82, 72, 82, 94, 9, FUR_DARK)
        self._blob(p, 82, 95, 5.5, 4, CREAM)
        # 胸前
        self._blob(p, 76, 72, 16, 24, FUR)
        self._blob(p, 72, 80, 9, 11, CREAM)
        # 近侧前爪抬起托腮
        paw_x = 51 + math.sin(t * 1.1) * 1.2
        self._capsule(p, 68, 72, paw_x + 4, 56, 8.5, FUR)
        self._blob(p, paw_x, 54, 5, 3.8, CREAM)
        # 颈部
        p.save()
        p.translate(60, 56 + bob)
        p.rotate(30 + tilt)
        self._blob(p, 0, 0, 9, 11, FUR)
        p.restore()

        self._collar(p, 61, 61 + bob)
        self._head(p, 52, 38 + bob, t,
                   eye_open=self._blink_openness(t),
                   ear_left=20 + tilt, ear_right=-20 + tilt * 0.5,
                   head_tilt=-6 + tilt)
        # 头顶漂浮的问号
        qa = int(150 + 60 * math.sin(t * 2.0))
        self._draw_question(p, 80, 20 + math.sin(t * 1.5) * 2.5, 10, qa)

    def _pose_work(self, p, t):
        """干活：前低后高的鞠躬姿势，前爪快速交替刨地，尘土飞扬"""
        speed = 13.0
        bob = math.sin(t * speed) * 0.8

        # 尾巴翘起快速摆动
        self._tail(p, 118, 42 + bob, 62 + math.sin(t * 10) * 14, 18, 6)
        # 后腿蹬地
        self._capsule(p, 106, 56, 114, 82, 9, FUR_DARK)
        self._blob(p, 115, 84, 5, 4, CREAM)
        self._capsule(p, 114, 54, 122, 80, 9, FUR)
        self._blob(p, 123, 82, 5, 4, CREAM)
        # 抬高的后半身与压低的肩部
        self._blob(p, 100, 50 + bob, 22, 14, FUR)
        self._blob(p, 90, 54 + bob, 14, 7, CREAM)
        self._blob(p, 74, 58 + bob, 18, 12, FUR)

        # 前爪交替快速刨地（伸到头前方的地面）
        def paw(phase, color):
            a = math.sin(t * speed + phase)
            x = 40 + a * 7
            y = 87 - abs(a) * 5
            self._capsule(p, 66, 64 + bob, x, y, 8, color)
            self._blob(p, x, y + 1, 4, 3.2, CREAM)

        paw(math.pi, FUR_DARK)
        paw(0.0, FUR)

        # 颈部
        p.save()
        p.translate(58, 52 + bob)
        p.rotate(30)
        self._blob(p, 0, 0, 8, 9, FUR)
        p.restore()

        self._collar(p, 54, 56 + bob, tilt=-24)
        self._head(p, 42, 44 + bob, t, eye_open=1.0,
                   ear_left=30, ear_right=-22, head_tilt=-4, tongue=True)

        # 扒出的尘土向前上扬
        for i in range(3):
            ph = (t * 1.6 + i / 3.0) % 1.0
            dust = QColor(180, 168, 150, int(150 * (1 - ph)))
            self._blob(p, 24 + ph * 10 + i * 3, 88 - ph * 14 - i * 2,
                       2.5 + ph * 3, 2 + ph * 2.5, dust)

    def _pose_happy(self, p, t):
        """开心：原地跳跃、前爪举起、耳朵飞起、星星闪烁"""
        hop = abs(math.sin(t * 4.2)) * 12.0
        yb = -hop
        wag = math.sin(t * 14) * 26.0

        # 影子随高度缩小
        p.setPen(Qt.NoPen)
        p.setBrush(SHADOW)
        sw = 1.0 - hop / 30.0
        p.drawEllipse(QRectF(84 - 40 * sw, 102, 80 * sw, 9))

        # 闪烁的小星星
        self._draw_star(p, 26, 42 + math.sin(t * 3.1) * 3, 6, 140 + 90 * math.sin(t * 5))
        self._draw_star(p, 134, 32 + math.cos(t * 2.6) * 3, 5, 140 + 90 * math.sin(t * 5 + 2))

        self._tail(p, 116, 60 + yb, 55 + wag, 20, 6.5)
        # 后腿向后蹬直
        self._capsule(p, 100, 68 + yb, 110, 84 + yb, 9, FUR_DARK)
        self._blob(p, 111, 85 + yb, 5, 4, CREAM)
        self._capsule(p, 104, 66 + yb, 114, 80 + yb, 9, FUR)
        self._blob(p, 115, 81 + yb, 5, 4, CREAM)
        # 身体
        self._blob(p, 82, 64 + yb, 27, 15, FUR)
        self._blob(p, 78, 70 + yb, 18, 7, CREAM)
        # 前爪高举
        self._capsule(p, 70, 62 + yb, 62, 46 + yb, 8.5, FUR_DARK)
        self._blob(p, 61, 44 + yb, 5, 4, CREAM)
        self._capsule(p, 64, 60 + yb, 54, 44 + yb, 8.5, FUR)
        self._blob(p, 53, 42 + yb, 5, 4, CREAM)
        # 颈部
        p.save()
        p.translate(58, 56 + yb)
        p.rotate(24)
        self._blob(p, 0, 0, 8, 9, FUR)
        p.restore()

        self._collar(p, 56, 60 + yb, tilt=-32)
        self._head(p, 46, 46 + yb, t, eye_open=1.0,
                   ear_left=-38 - abs(math.sin(t * 4.2)) * 10,
                   ear_right=-56 - abs(math.sin(t * 4.2)) * 10,
                   tongue=True, head_tilt=-8)

    def _pose_sad(self, p, t):
        """失落：垂耳低头、尾巴拖地、头顶冒汗"""
        droop = 4 + math.sin(t * 1.0)
        wag = math.sin(t * 1.4) * 3.0

        # 尾巴无精打采地拖在地上
        self._tail(p, 122, 82, 10 + wag, 18, 6)
        self._blob(p, 100, 82, 25, 17, FUR)
        self._blob(p, 108, 96, 8, 5.5, CREAM)
        self._capsule(p, 80, 74, 80, 96, 9, FUR_DARK)
        self._blob(p, 80, 97, 5.5, 4, CREAM)
        self._blob(p, 76, 76, 16, 22, FUR)
        self._blob(p, 72, 84, 9, 10, CREAM)
        self._capsule(p, 68, 74, 68, 97, 9, FUR)
        self._blob(p, 68, 98, 5.5, 4, CREAM)

        p.save()
        p.translate(62, 62 + droop)
        p.rotate(40)
        self._blob(p, 0, 0, 9, 11, FUR)
        p.restore()

        self._collar(p, 63, 66 + droop)
        self._head(p, 54, 46 + droop, t, eye_open=0.35,
                   ear_left=36, ear_right=-8, head_tilt=10)
        # 头顶的汗滴
        self._draw_sweat(p, 78, 24 + math.sin(t * 2.0), 6, 170)

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
        stretch_action = menu.addAction("🐕 伸个懒腰")
        run_action = menu.addAction("💨 跑一会儿")
        menu.addSeparator()
        exit_action = menu.addAction("🐶 退出程序")
        action = menu.exec(event.globalPos())
        if action == stretch_action:
            self._enter_pose("stretch", 2.2)
        elif action == run_action:
            self._enter_pose("run", 6.0)
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
