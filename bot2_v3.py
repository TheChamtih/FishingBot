import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional, Tuple

import cv2
import keyboard
import mss
import numpy as np
import pyautogui


# Fast mouse actions without built-in delays.
pyautogui.PAUSE = 0
pyautogui.MINIMUM_DURATION = 0


@dataclass
class ColorThresholds:
    dark_green_b_min: int = 5
    dark_green_b_max: int = 130
    dark_green_g_min: int = 65
    dark_green_g_max: int = 180
    dark_green_r_min: int = 0
    dark_green_r_max: int = 115
    dark_green_dom: int = 12

    bright_green_b_min: int = 0
    bright_green_b_max: int = 95
    bright_green_g_min: int = 120
    bright_green_g_max: int = 255
    bright_green_r_min: int = 0
    bright_green_r_max: int = 95
    bright_green_dom: int = 18

    red_r_min: int = 90
    red_g_max: int = 125
    red_b_max: int = 125
    red_dom: int = 35

    white_min: int = 120
    white_peak_min: int = 130
    white_spread_max: int = 50

    hsv_green_h_min: int = 35
    hsv_green_h_max: int = 95
    hsv_green_s_min: int = 45
    hsv_green_v_min: int = 45

    hsv_red_h1_max: int = 12
    hsv_red_h2_min: int = 160
    hsv_red_s_min: int = 55
    hsv_red_v_min: int = 45


@dataclass
class BotConfig:
    monitor_index: int = 1

    search_bar_w: int = 760
    search_bar_h: int = 360
    search_bar_y_offset: int = 50

    scale_search_w: int = 420
    scale_search_h: int = 520

    green_confirm_frames: int = 2
    scale_confirm_frames: int = 4
    lost_scale_frames: int = 10

    wait_scale_timeout_sec: float = 180.0
    wait_scale_warmup_sec: float = 1.25
    wait_scale_min_accept_sec: float = 2.8
    play_scale_timeout_sec: float = 60.0
    play_target_pos: float = 0.28
    play_deadband_half: float = 0.055
    play_control_smooth_alpha: float = 0.45
    play_prediction_gain: float = 0.85
    play_click_min_interval_sec: float = 0.020
    play_click_max_interval_sec: float = 0.085
    play_lost_float_safe_click_interval_sec: float = 0.085
    post_click_delay_sec: float = 0.18
    post_cycle_cooldown_sec: float = 3.0

    scale_stable_x_tol: int = 6
    scale_stable_top_tol: int = 10
    scale_stable_height_tol: int = 18
    scale_center_x_max_offset: int = 70
    scale_min_height_px: int = 120
    scale_max_height_px: int = 420
    scale_min_column_green_px: int = 6
    scale_min_width_px: int = 6
    scale_max_width_px: int = 24
    scale_min_red_bottom_ratio: float = 0.10
    scale_max_red_upper_ratio: float = 0.06
    scale_min_green_upper_ratio: float = 0.08
    scale_visible_ratio_min: float = 0.10
    ui_topbar_half_w: int = 420
    ui_topbar_scan_h: int = 320
    ui_topbar_left_right_red_min: float = 0.12
    ui_topbar_center_green_min: float = 0.20
    ui_topbar_center_red_max: float = 0.22
    ui_topbar_score_accept: float = 0.50
    ui_topbar_score_strict: float = 0.65
    ui_topbar_confirm_frames: int = 2
    ui_topbar_seen_hold_sec: float = 1.2
    fallback_scale_min_height_px: int = 175
    fallback_scale_min_visible_ratio: float = 0.14
    fallback_float_min_pos: float = 0.04
    fallback_float_max_pos: float = 0.93
    scale_min_float_pos: float = 0.01
    scale_max_float_pos: float = 0.97

    hook_min_margin_px: int = 6
    hook_min_green_ratio: float = 0.14
    hook_prediction_gain: float = 1.2
    hook_prediction_max_px: int = 28

    debug_wait_log_interval_sec: float = 0.35
    debug_wait_save_reject_every: int = 6

    fps_inactive: float = 10.0
    fps_search_bar: float = 45.0
    fps_wait_scale: float = 20.0
    fps_play_scale: float = 80.0

    debug_dir: str = "debug_v3"


class BotState(Enum):
    SEARCH_BAR = auto()
    WAIT_SCALE = auto()
    PLAY_SCALE = auto()


class FrameLimiter:
    def __init__(self, fps: float) -> None:
        self.target_dt = 1.0 / max(1.0, fps)
        self.next_tick = time.perf_counter()

    def wait(self) -> None:
        now = time.perf_counter()
        if self.next_tick > now:
            time.sleep(self.next_tick - now)
        self.next_tick = max(self.next_tick + self.target_dt, time.perf_counter())


class ColorDetector:
    def __init__(self, thresholds: ColorThresholds) -> None:
        self.t = thresholds
        self.green_shift = 0

    @staticmethod
    def _split_i16(roi_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        b = roi_bgr[:, :, 0].astype(np.int16)
        g = roi_bgr[:, :, 1].astype(np.int16)
        r = roi_bgr[:, :, 2].astype(np.int16)
        return b, g, r

    def adapt(self, frame_bgr: np.ndarray) -> None:
        """
        Lightweight adaptation for brightness changes.
        The shift is intentionally conservative to avoid drift.
        """
        h, w = frame_bgr.shape[:2]
        x0 = max(0, w // 2 - 120)
        x1 = min(w, w // 2 + 120)
        y0 = max(0, h // 2 - 120)
        y1 = min(h, h // 2 + 120)
        roi = frame_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return

        b, g, r = self._split_i16(roi)
        green_like = (g > b + 8) & (g > r + 8)
        if not np.any(green_like):
            return

        g_vals = g[green_like]
        if g_vals.size < 200:
            return

        g_median = int(np.median(g_vals))
        target = 145
        delta = np.clip((target - g_median) // 8, -10, 10)
        self.green_shift = int(np.clip(self.green_shift * 0.8 + delta * 0.2, -12, 12))

    def green_mask(self, roi_bgr: np.ndarray) -> np.ndarray:
        b, g, r = self._split_i16(roi_bgr)
        shift = self.green_shift

        dark_green = (
            (b >= self.t.dark_green_b_min) & (b <= self.t.dark_green_b_max)
            & (g >= self.t.dark_green_g_min + shift) & (g <= self.t.dark_green_g_max)
            & (r >= self.t.dark_green_r_min) & (r <= self.t.dark_green_r_max)
            & (g >= b + self.t.dark_green_dom)
            & (g >= r + self.t.dark_green_dom)
        )

        bright_green = (
            (b >= self.t.bright_green_b_min) & (b <= self.t.bright_green_b_max)
            & (g >= self.t.bright_green_g_min + shift) & (g <= self.t.bright_green_g_max)
            & (r >= self.t.bright_green_r_min) & (r <= self.t.bright_green_r_max)
            & (g >= b + self.t.bright_green_dom)
            & (g >= r + self.t.bright_green_dom)
        )

        base = dark_green | bright_green
        if float(np.mean(base)) < 0.02:
            hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
            h = hsv[:, :, 0]
            s = hsv[:, :, 1]
            v = hsv[:, :, 2]
            hsv_green = (
                (h >= self.t.hsv_green_h_min)
                & (h <= self.t.hsv_green_h_max)
                & (s >= self.t.hsv_green_s_min)
                & (v >= self.t.hsv_green_v_min)
            )
            return base | hsv_green
        return base

    def red_mask(self, roi_bgr: np.ndarray) -> np.ndarray:
        b, g, r = self._split_i16(roi_bgr)
        bgr_red = (
            (r >= self.t.red_r_min)
            & (g <= self.t.red_g_max)
            & (b <= self.t.red_b_max)
            & (r >= g + self.t.red_dom)
            & (r >= b + self.t.red_dom)
        )

        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0]
        s = hsv[:, :, 1]
        v = hsv[:, :, 2]
        hsv_red = (
            ((h <= self.t.hsv_red_h1_max) | (h >= self.t.hsv_red_h2_min))
            & (s >= self.t.hsv_red_s_min)
            & (v >= self.t.hsv_red_v_min)
        )

        return bgr_red | hsv_red

    def white_mask(self, roi_bgr: np.ndarray) -> np.ndarray:
        b, g, r = self._split_i16(roi_bgr)
        max_c = np.maximum(np.maximum(b, g), r)
        min_c = np.minimum(np.minimum(b, g), r)
        spread = max_c - min_c
        return (min_c > self.t.white_min) & (max_c >= self.t.white_peak_min) & (spread <= self.t.white_spread_max)


class FishingBotV3:
    def __init__(self, config: Optional[BotConfig] = None, thresholds: Optional[ColorThresholds] = None) -> None:
        self.cfg = config or BotConfig()
        self.detector = ColorDetector(thresholds or ColorThresholds())

        self.active = False
        self.debug_mode = False
        self.state = BotState.SEARCH_BAR

        self.hook_history: deque = deque(maxlen=3)
        self.green_confirm_count = 0
        self.last_hook_pos: Optional[Tuple[int, int]] = None

        self.scale_confirm_count = 0
        self.last_scale_candidate: Optional[dict] = None
        self.scale_wait_start = 0.0
        self.play_scale_start = 0.0
        self.scale_lost_count = 0
        self.clicks_count = 0
        self.scale_reject_count = 0
        self.last_wait_debug_log_ts = 0.0
        self.topbar_seen_until_ts = 0.0
        self.topbar_confirm_count = 0
        self.play_last_click_ts = 0.0
        self.play_float_ema: Optional[float] = None
        self.play_last_float_raw: Optional[float] = None
        self.cycles_total = 0
        self.fish_caught_total = 0

        self.hotkey_exit_requested = False
        self.hotkey_toggle_active_requested = False
        self.hotkey_toggle_debug_requested = False
        self.hotkey_last_active_ts = 0.0
        self.hotkey_last_debug_ts = 0.0
        self.hotkey_last_exit_ts = 0.0
        self.hotkey_debounce_sec = 0.18
        self.hotkey_handles = []
        self.hotkeys_registered = False

        self.cursor_corner_step = 0

        self.limiters = {
            "inactive": FrameLimiter(self.cfg.fps_inactive),
            BotState.SEARCH_BAR: FrameLimiter(self.cfg.fps_search_bar),
            BotState.WAIT_SCALE: FrameLimiter(self.cfg.fps_wait_scale),
            BotState.PLAY_SCALE: FrameLimiter(self.cfg.fps_play_scale),
        }

        self.logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger("fishing_bot_v3")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            logger.addHandler(handler)
        return logger

    def _on_hotkey_exit(self) -> None:
        now = time.perf_counter()
        if now - self.hotkey_last_exit_ts < self.hotkey_debounce_sec:
            return
        self.hotkey_last_exit_ts = now
        self.hotkey_exit_requested = True

    def _on_hotkey_toggle_active(self) -> None:
        now = time.perf_counter()
        if now - self.hotkey_last_active_ts < self.hotkey_debounce_sec:
            return
        self.hotkey_last_active_ts = now
        self.hotkey_toggle_active_requested = True

    def _on_hotkey_toggle_debug(self) -> None:
        now = time.perf_counter()
        if now - self.hotkey_last_debug_ts < self.hotkey_debounce_sec:
            return
        self.hotkey_last_debug_ts = now
        self.hotkey_toggle_debug_requested = True

    def _register_hotkeys(self) -> None:
        self._unregister_hotkeys()
        bindings = [
            ("8", self._on_hotkey_exit),
            ("num 8", self._on_hotkey_exit),
            ("esc", self._on_hotkey_exit),
            ("7", self._on_hotkey_toggle_active),
            ("num 7", self._on_hotkey_toggle_active),
            ("0", self._on_hotkey_toggle_debug),
            ("num 0", self._on_hotkey_toggle_debug),
        ]

        for hotkey, callback in bindings:
            try:
                handle = keyboard.add_hotkey(hotkey, callback, suppress=False, trigger_on_release=False)
                self.hotkey_handles.append(handle)
            except Exception as exc:
                self.logger.warning("Hotkey registration failed for '%s': %s", hotkey, exc)

        self.hotkeys_registered = len(self.hotkey_handles) > 0
        if self.hotkeys_registered:
            self.logger.info("Hotkeys ready: 7/num7 start-stop, 8/num8/esc exit, 0/num0 debug")
        else:
            self.logger.warning("Hotkeys unavailable, fallback to key polling")

    def _unregister_hotkeys(self) -> None:
        for handle in self.hotkey_handles:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:
                pass
        self.hotkey_handles.clear()
        self.hotkeys_registered = False

    def _save_debug_frame(self, frame_bgr: np.ndarray, tag: str) -> None:
        if not self.debug_mode:
            return
        out_dir = Path(self.cfg.debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        path = out_dir / f"{ts}_{tag}.png"
        cv2.imwrite(str(path), frame_bgr)

    def _capture_screen(self, sct: mss.mss) -> np.ndarray:
        monitor = sct.monitors[self.cfg.monitor_index]
        frame = np.array(sct.grab(monitor))
        return frame[:, :, :3]

    @staticmethod
    def _smooth_position(pos: Optional[Tuple[int, int]], history: deque) -> Optional[Tuple[int, int]]:
        if pos is None:
            history.clear()
            return None
        history.append(pos)
        x_vals = np.array([p[0] for p in history])
        y_vals = np.array([p[1] for p in history])
        return int(np.median(x_vals)), int(np.median(y_vals))

    def _park_cursor_in_corner(self) -> None:
        sw, sh = pyautogui.size()
        pad = 8
        corners = [
            (pad, pad),
            (sw - pad, pad),
            (sw - pad, sh - pad),
            (pad, sh - pad),
        ]
        x, y = corners[self.cursor_corner_step % len(corners)]
        self.cursor_corner_step += 1
        pyautogui.moveTo(x, y)

    def _find_horizontal_bar(self, screen_bgr: np.ndarray) -> Tuple[Optional[Tuple[int, int]], int]:
        h, w = screen_bgr.shape[:2]
        cx = w // 2
        cy = min(h - 1, h // 2 + self.cfg.search_bar_y_offset)

        x0 = max(0, cx - self.cfg.search_bar_w // 2)
        x1 = min(w, cx + self.cfg.search_bar_w // 2)
        y0 = max(0, cy - self.cfg.search_bar_h // 2)
        y1 = min(h, cy + self.cfg.search_bar_h // 2)

        roi = screen_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return None, 0

        roi_h, roi_w = roi.shape[:2]
        green = self.detector.green_mask(roi)
        white = self.detector.white_mask(roi)
        red = self.detector.red_mask(roi)

        mouse_x, mouse_y = pyautogui.position()
        mx = int(mouse_x - x0)
        my = int(mouse_y - y0)
        if 0 <= mx < roi_w and 0 <= my < roi_h:
            pad = 18
            white[max(0, my - pad):min(roi_h, my + pad + 1), max(0, mx - pad):min(roi_w, mx + pad + 1)] = False

        green_u8 = (green.astype(np.uint8) * 255)
        kernel = np.ones((3, 3), np.uint8)
        green_u8 = cv2.morphologyEx(green_u8, cv2.MORPH_OPEN, kernel)
        green_u8 = cv2.morphologyEx(green_u8, cv2.MORPH_CLOSE, kernel)
        green = green_u8 > 0

        best = None
        best_score = -10**9
        center_x = roi_w // 2
        center_y = roi_h // 2

        for y in range(4, roi_h - 4, 2):
            ys = slice(y - 2, y + 3)
            green_band = np.any(green[ys, :], axis=0)
            white_band = np.any(white[ys, :], axis=0)
            red_band = np.any(red[ys, :], axis=0)

            red_padded = np.pad(red_band.astype(np.int8), (1, 1), mode="constant")
            red_trans = np.diff(red_padded)
            red_starts = np.where(red_trans == 1)[0]
            red_ends = np.where(red_trans == -1)[0] - 1

            if len(red_starts) < 2:
                continue

            for i in range(len(red_starts) - 1):
                left_s = int(red_starts[i])
                left_e = int(red_ends[i])
                right_s = int(red_starts[i + 1])
                right_e = int(red_ends[i + 1])

                core_s = left_e + 1
                core_e = right_s - 1
                core_len = core_e - core_s + 1
                if core_len < 65 or core_len > 430:
                    continue

                core_green = green_band[core_s:core_e + 1]
                if core_green.size == 0:
                    continue

                line_ratio = float(np.mean(core_green))
                fill_ratio = float(np.mean(green[ys, core_s:core_e + 1]))
                left_red_ratio = float(np.mean(red[ys, left_s:left_e + 1])) if left_e >= left_s else 0.0
                right_red_ratio = float(np.mean(red[ys, right_s:right_e + 1])) if right_e >= right_s else 0.0

                if line_ratio < 0.58 or fill_ratio < 0.36:
                    continue
                if left_red_ratio < 0.22 or right_red_ratio < 0.22:
                    continue

                seg_white = white_band[core_s:core_e + 1]
                if not np.any(seg_white):
                    continue

                wp = np.pad(seg_white.astype(np.int8), (1, 1), mode="constant")
                wt = np.diff(wp)
                w_starts = np.where(wt == 1)[0]
                w_ends = np.where(wt == -1)[0] - 1

                candidate_x = None
                candidate_score = -10**9
                core_center = (core_s + core_e) // 2

                for ws, we in zip(w_starts, w_ends):
                    run_len = int(we - ws + 1)
                    if run_len < 1 or run_len > 16:
                        continue

                    wx = int(core_s + (ws + we) // 2)
                    if wx - core_s < 3 or core_e - wx < 3:
                        continue

                    wx0 = max(0, wx - 2)
                    wx1 = min(roi_w, wx + 3)
                    wy0 = max(0, y - 12)
                    wy1 = min(roi_h, y + 13)
                    white_col = white[wy0:wy1, wx0:wx1]
                    white_vertical = int(np.sum(np.any(white_col, axis=1))) if white_col.size > 0 else 0
                    if white_vertical < 2 or white_vertical > 20:
                        continue

                    score = 120 - abs(wx - core_center) - run_len
                    if score > candidate_score:
                        candidate_score = score
                        candidate_x = wx

                if candidate_x is None:
                    continue

                score = 0
                score += min(core_len, 240)
                score += int(fill_ratio * 160)
                score += int(line_ratio * 120)
                score += int((left_red_ratio + right_red_ratio) * 180)
                score -= abs(((core_s + core_e) // 2) - center_x) * 2
                score -= abs(y - center_y) * 3

                if score > best_score:
                    best_score = score
                    best = (candidate_x, y, core_s)

        if best is None:
            return None, 0

        wx, y, core_s = best
        rx0 = max(0, wx - 8)
        rx1 = min(roi_w, wx + 9)
        ry0 = max(0, y - 10)
        ry1 = min(roi_h, y + 11)
        local = white[ry0:ry1, rx0:rx1]
        ys, _ = np.where(local)
        if len(ys) > 0:
            y = int(ry0 + np.median(ys))

        abs_x = wx + x0
        abs_y = y + y0
        bar_width = max(0, wx - core_s)
        return (abs_x, abs_y), bar_width

    def _check_hook_in_green(self, screen_bgr: np.ndarray, hook_pos: Tuple[int, int]) -> Tuple[bool, float]:
        x, y = hook_pos
        x0 = max(0, x - 140)
        x1 = min(screen_bgr.shape[1], x + 141)
        y0 = max(0, y - 4)
        y1 = min(screen_bgr.shape[0], y + 5)

        roi = screen_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return False, 0.0

        green = self.detector.green_mask(roi)
        red = self.detector.red_mask(roi)

        cx = x - x0
        cy = y - y0
        cx0 = max(0, cx - 11)
        cx1 = min(roi.shape[1], cx + 12)
        cy0 = max(0, cy - 4)
        cy1 = min(roi.shape[0], cy + 5)

        center_green = green[cy0:cy1, cx0:cx1]
        center_red = red[cy0:cy1, cx0:cx1]

        center_green_ratio = float(np.mean(center_green)) if center_green.size > 0 else 0.0
        center_red_ratio = float(np.mean(center_red)) if center_red.size > 0 else 0.0

        green_band = np.any(green, axis=0)
        red_band = np.any(red, axis=0)

        padded = np.pad(green_band.astype(np.int8), (1, 1), mode="constant")
        transitions = np.diff(padded)
        starts = np.where(transitions == 1)[0]
        ends = np.where(transitions == -1)[0] - 1

        in_green_segment = False
        nearest_seg = None

        if len(starts) > 0:
            centers = (starts + ends) // 2
            idx = int(np.argmin(np.abs(centers - cx)))
            s = int(starts[idx])
            e = int(ends[idx])
            nearest_seg = (s, e)
            seg_len = max(1, e - s + 1)
            edge_margin = max(3, int(seg_len * 0.10))
            in_green_segment = (s + edge_margin) <= cx <= (e - edge_margin)

        red_indices = np.where(red_band)[0]
        nearest_red_dist = int(np.min(np.abs(red_indices - cx))) if len(red_indices) > 0 else 999

        tx0 = max(0, cx - 7)
        tx1 = min(roi.shape[1], cx + 8)
        tight_green_ratio = float(np.mean(green[cy0:cy1, tx0:tx1])) if tx1 > tx0 else 0.0
        tight_red_ratio = float(np.mean(red[cy0:cy1, tx0:tx1])) if tx1 > tx0 else 0.0

        local_green_ok = (
            (center_green_ratio >= 0.12 and center_green_ratio >= center_red_ratio * 1.35)
            or (center_green_ratio >= 0.20 and center_red_ratio < 0.12)
        )

        segment_has_safe_margin = False
        if nearest_seg is not None:
            s, e = nearest_seg
            margin = self.cfg.hook_min_margin_px
            segment_has_safe_margin = (cx - s >= margin) and (e - cx >= margin)

        red_safety_ok = (tight_red_ratio < 0.08) and (nearest_red_dist >= 8)
        green_strength_ok = tight_green_ratio >= self.cfg.hook_min_green_ratio

        in_green = in_green_segment and segment_has_safe_margin and local_green_ok and red_safety_ok and green_strength_ok
        return in_green, center_green_ratio

    def _find_vertical_scale(self, screen_bgr: np.ndarray) -> Optional[dict]:
        h, w = screen_bgr.shape[:2]

        cx = w // 2
        cy = h // 2
        x0 = max(0, cx - self.cfg.scale_search_w // 2)
        x1 = min(w, cx + self.cfg.scale_search_w // 2)
        y0 = max(0, cy - self.cfg.scale_search_h // 2)
        y1 = min(h, cy + self.cfg.scale_search_h // 2)

        roi = screen_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return None

        green = self.detector.green_mask(roi)
        if not np.any(green):
            return None
        red = self.detector.red_mask(roi)

        roi_h, roi_w = roi.shape[:2]
        green_columns = np.where(np.any(green, axis=0))[0]
        if len(green_columns) == 0:
            return None

        center_roi_x = roi_w // 2
        sorted_cols = sorted(green_columns, key=lambda x: abs(int(x) - center_roi_x))

        for x_roi in sorted_cols[::3]:
            green_y = np.where(green[:, x_roi])[0]
            red_y = np.where(red[:, x_roi])[0]

            if len(green_y) < 3 or len(red_y) == 0:
                continue

            top_green = int(green_y[0])
            red_below = red_y[red_y > top_green + 60]
            if len(red_below) == 0:
                continue

            bottom_red = int(red_below[0])
            height = bottom_red - top_green
            if not (self.cfg.scale_min_height_px <= height <= self.cfg.scale_max_height_px):
                continue

            green_count = int(np.sum(green[top_green:top_green + 80, x_roi]))
            if green_count < self.cfg.scale_min_column_green_px:
                continue

            xw0 = max(0, int(x_roi) - 8)
            xw1 = min(roi_w, int(x_roi) + 9)
            yw0 = max(0, top_green)
            yw1 = min(roi_h, bottom_red + 1)
            if yw1 <= yw0:
                continue

            col_activity = np.sum(green[yw0:yw1, xw0:xw1], axis=0)
            active_cols = np.where(col_activity >= 6)[0]
            if len(active_cols) == 0:
                continue

            width = int(active_cols[-1] - active_cols[0] + 1)
            if width < self.cfg.scale_min_width_px or width > self.cfg.scale_max_width_px:
                continue

            red_bottom = float(np.mean(red[max(0, bottom_red - 8):min(roi_h, bottom_red + 9), xw0:xw1]))
            if red_bottom < self.cfg.scale_min_red_bottom_ratio:
                continue

            upper_h = max(10, int(height * 0.65))
            upper_y1 = min(roi_h, top_green + upper_h)
            upper_green_ratio = float(np.mean(green[top_green:upper_y1, xw0:xw1]))
            upper_red_ratio = float(np.mean(red[top_green:upper_y1, xw0:xw1]))
            if upper_green_ratio < self.cfg.scale_min_green_upper_ratio:
                continue
            if upper_red_ratio > self.cfg.scale_max_red_upper_ratio:
                continue

            abs_x = int(x_roi + x0)
            if abs(abs_x - (w // 2)) > self.cfg.scale_center_x_max_offset:
                continue

            return {
                "x": abs_x,
                "top_y": int(top_green + y0),
                "height": int(height),
                "green_y": int(top_green + y0),
                "red_y": int(bottom_red + y0),
            }

        return None

    def _score_minigame_top_bar(self, screen_bgr: np.ndarray, scale_x: int) -> float:
        h, w = screen_bgr.shape[:2]
        half_w = min(self.cfg.ui_topbar_half_w, w // 2 - 4)
        if half_w < 120:
            return 0.0

        x0 = max(0, int(scale_x) - half_w)
        x1 = min(w, int(scale_x) + half_w)
        y0 = 0
        y1 = min(h, self.cfg.ui_topbar_scan_h)
        if x1 - x0 < 240 or y1 - y0 < 20:
            return 0.0

        roi = screen_bgr[y0:y1, x0:x1]
        green = self.detector.green_mask(roi)
        red = self.detector.red_mask(roi)
        roi_h, roi_w = roi.shape[:2]

        left_end = max(1, int(roi_w * 0.18))
        center_start = int(roi_w * 0.28)
        center_end = int(roi_w * 0.72)
        right_start = min(roi_w - 1, int(roi_w * 0.82))
        if center_end <= center_start or right_start <= left_end:
            return 0.0

        best_score = 0.0

        for y in range(6, roi_h - 6, 3):
            ys = slice(y - 3, y + 4)
            green_line = np.any(green[ys, :], axis=0)
            red_line = np.any(red[ys, :], axis=0)
            color_line = green_line | red_line

            left_red = float(np.mean(red_line[:left_end]))
            right_red = float(np.mean(red_line[right_start:]))
            center_green = float(np.mean(green_line[center_start:center_end]))
            center_red = float(np.mean(red_line[center_start:center_end]))
            center_color = float(np.mean(color_line[center_start:center_end]))

            left_score = min(1.0, left_red / max(0.01, self.cfg.ui_topbar_left_right_red_min * 0.8))
            right_score = min(1.0, right_red / max(0.01, self.cfg.ui_topbar_left_right_red_min * 0.8))
            green_score = min(1.0, center_green / max(0.01, self.cfg.ui_topbar_center_green_min * 0.65))
            red_penalty = min(1.0, center_red / max(0.01, self.cfg.ui_topbar_center_red_max * 1.45))
            color_score = min(1.0, center_color / 0.18)

            score = (
                0.24 * left_score
                + 0.24 * right_score
                + 0.36 * green_score
                + 0.16 * color_score
                - 0.22 * red_penalty
            )
            if score > best_score:
                best_score = score

        return float(np.clip(best_score, 0.0, 1.0))

    def _find_float_on_scale(self, screen_bgr: np.ndarray, scale: dict) -> Tuple[Optional[float], str]:
        x = scale["x"]
        top_y = scale["top_y"]
        height = scale["height"]

        x0 = max(0, x - 15)
        x1 = min(screen_bgr.shape[1], x + 16)
        y0 = max(0, top_y)
        y1 = min(screen_bgr.shape[0], top_y + height)

        roi = screen_bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return None, "unknown"

        white = self.detector.white_mask(roi)
        row_strength = np.sum(white, axis=1).astype(np.int32)
        min_row_pixels = max(2, roi.shape[1] // 8)
        rows = np.where(row_strength >= min_row_pixels)[0]
        if len(rows) == 0:
            rows = np.where(np.any(white, axis=1))[0]
            if len(rows) == 0:
                return None, "unknown"

        runs = []
        start = int(rows[0])
        prev = int(rows[0])
        for r in rows[1:]:
            r_int = int(r)
            if r_int == prev + 1:
                prev = r_int
                continue
            runs.append((start, prev))
            start = r_int
            prev = r_int
        runs.append((start, prev))

        edge_guard = max(1, roi.shape[0] // 50)

        def run_mass(run: Tuple[int, int]) -> int:
            rs, re = run
            return int(np.sum(row_strength[rs:re + 1]))

        non_edge_runs = [run for run in runs if run[0] > edge_guard and run[1] < (roi.shape[0] - 1 - edge_guard)]
        candidate_runs = non_edge_runs if non_edge_runs else runs
        best_start, best_end = max(candidate_runs, key=run_mass)

        y_abs = int((best_start + best_end) // 2 + y0)
        relative = (y_abs - top_y) / max(1, height)

        if relative < 0.40:
            zone = "green"
        elif relative < 0.65:
            zone = "gray"
        else:
            zone = "red"

        return float(relative), zone

    def _finalize_cycle(self, fish_caught: bool, reason: str) -> None:
        self.cycles_total += 1
        if fish_caught:
            self.fish_caught_total += 1

        print(f"\nCycle #{self.cycles_total} ({reason}) | Total fish: {self.fish_caught_total}")
        self.logger.info(
            "Cycle #%d finished (%s). Total fish caught: %d",
            self.cycles_total,
            reason,
            self.fish_caught_total,
        )

    def _transition_to_wait_scale(self) -> None:
        self.state = BotState.WAIT_SCALE
        self.scale_wait_start = time.time()
        self.scale_confirm_count = 0
        self.last_scale_candidate = None
        self.scale_reject_count = 0
        self.last_wait_debug_log_ts = 0.0
        self.topbar_seen_until_ts = 0.0
        self.topbar_confirm_count = 0

    def _transition_to_play_scale(self) -> None:
        self.state = BotState.PLAY_SCALE
        self.play_scale_start = time.time()
        self.scale_lost_count = 0
        self.clicks_count = 0
        self.play_last_click_ts = 0.0
        self.play_float_ema = None
        self.play_last_float_raw = None
        self.last_scale_candidate = None
        self.scale_reject_count = 0
        self.topbar_confirm_count = 0

    def _transition_to_search(self) -> None:
        self.state = BotState.SEARCH_BAR
        self.green_confirm_count = 0
        self.last_hook_pos = None
        self.scale_confirm_count = 0
        self.last_scale_candidate = None
        self.scale_lost_count = 0
        self.scale_reject_count = 0
        self.topbar_seen_until_ts = 0.0
        self.topbar_confirm_count = 0
        self.play_last_click_ts = 0.0
        self.play_float_ema = None
        self.play_last_float_raw = None

    def _play_click_controller(
        self,
        float_pos: float,
        now_ts: float,
    ) -> Tuple[bool, float, float, float, float, float]:
        alpha = float(np.clip(self.cfg.play_control_smooth_alpha, 0.05, 1.0))
        if self.play_float_ema is None:
            ema = float_pos
        else:
            ema = (1.0 - alpha) * self.play_float_ema + alpha * float_pos

        velocity = 0.0 if self.play_last_float_raw is None else (float_pos - self.play_last_float_raw)
        predicted = float(np.clip(ema + velocity * self.cfg.play_prediction_gain, 0.0, 1.0))

        target = float(np.clip(self.cfg.play_target_pos, 0.02, 0.98))
        half = max(0.01, float(self.cfg.play_deadband_half))
        lower = max(0.0, target - half)
        upper = min(1.0, target + half)

        should_click = predicted > upper
        click_interval = self.cfg.play_click_max_interval_sec

        if should_click:
            err = predicted - upper
            span = max(1e-4, 1.0 - upper)
            urgency = float(np.clip(err / span, 0.0, 1.0))
            click_interval = (
                self.cfg.play_click_max_interval_sec
                - urgency * (self.cfg.play_click_max_interval_sec - self.cfg.play_click_min_interval_sec)
            )

        can_click_now = should_click and ((now_ts - self.play_last_click_ts) >= click_interval)

        self.play_float_ema = float(ema)
        self.play_last_float_raw = float(float_pos)
        return can_click_now, float(ema), predicted, lower, upper, float(click_interval)

    def _debug_wait_scale_log(
        self,
        elapsed: float,
        scale: Optional[dict],
        float_pos: Optional[float],
        stable_ok: bool,
        reason: str,
    ) -> None:
        if not self.debug_mode:
            return

        now = time.time()
        if now - self.last_wait_debug_log_ts < self.cfg.debug_wait_log_interval_sec:
            return
        self.last_wait_debug_log_ts = now

        if scale is None:
            self.logger.info(
                "[WAIT-DBG] t=%.1fs scale=none confirm=%d/%d reason=%s",
                elapsed,
                self.scale_confirm_count,
                self.cfg.scale_confirm_frames,
                reason,
            )
            return

        float_txt = "none" if float_pos is None else f"{float_pos:.3f}"
        self.logger.info(
            "[WAIT-DBG] t=%.1fs x=%d top=%d h=%d float=%s stable=%s confirm=%d/%d reason=%s",
            elapsed,
            scale["x"],
            scale["top_y"],
            scale["height"],
            float_txt,
            "yes" if stable_ok else "no",
            self.scale_confirm_count,
            self.cfg.scale_confirm_frames,
            reason,
        )

    def _debug_wait_scale_snapshot(self, frame: np.ndarray, scale: Optional[dict], reason: str) -> None:
        if not self.debug_mode:
            return

        self.scale_reject_count += 1
        if self.scale_reject_count % self.cfg.debug_wait_save_reject_every != 0:
            return

        debug = frame.copy()
        cv2.putText(
            debug,
            f"WAIT_SCALE REJECT: {reason}",
            (16, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 255),
            2,
        )
        if scale is not None:
            x = int(scale["x"])
            top = int(scale["top_y"])
            h = int(scale["height"])
            cv2.line(debug, (x, top), (x, top + h), (0, 255, 255), 2)
            cv2.circle(debug, (x, top), 6, (0, 255, 0), -1)
            cv2.circle(debug, (x, top + h), 6, (0, 0, 255), -1)

        self._save_debug_frame(debug, f"wait_reject_{reason}")

    def _is_stable_scale(self, scale: dict) -> bool:
        prev = self.last_scale_candidate
        self.last_scale_candidate = scale
        if prev is None:
            return True

        dx = abs(scale["x"] - prev["x"])
        dtop = abs(scale["top_y"] - prev["top_y"])
        dh = abs(scale["height"] - prev["height"])
        return (
            dx <= self.cfg.scale_stable_x_tol
            and dtop <= self.cfg.scale_stable_top_tol
            and dh <= self.cfg.scale_stable_height_tol
        )

    def _process_search_bar(self, frame: np.ndarray) -> None:
        hook_pos, bar_width = self._find_horizontal_bar(frame)
        hook_pos = self._smooth_position(hook_pos, self.hook_history)

        if hook_pos is None:
            self.green_confirm_count = 0
            self.last_hook_pos = None
            print("Searching bar...      ", end="\r")
            return

        in_green, green_ratio = self._check_hook_in_green(frame, hook_pos)

        predicted_pos = hook_pos
        if self.last_hook_pos is not None:
            dx = hook_pos[0] - self.last_hook_pos[0]
            pred_dx = int(np.clip(dx * self.cfg.hook_prediction_gain, -self.cfg.hook_prediction_max_px, self.cfg.hook_prediction_max_px))
            predicted_x = int(np.clip(hook_pos[0] + pred_dx, 0, frame.shape[1] - 1))
            predicted_pos = (predicted_x, hook_pos[1])

        pred_in_green, pred_green_ratio = self._check_hook_in_green(frame, predicted_pos)
        click_pos = predicted_pos if (pred_in_green and pred_green_ratio >= green_ratio) else hook_pos
        best_in_green = pred_in_green if click_pos == predicted_pos else in_green
        best_green_ratio = pred_green_ratio if click_pos == predicted_pos else green_ratio

        click_candidate = best_in_green and (10 <= bar_width <= 220) and (best_green_ratio >= self.cfg.hook_min_green_ratio)
        if click_candidate:
            if self.last_hook_pos is None:
                stable_pos = True
            else:
                dx = abs(hook_pos[0] - self.last_hook_pos[0])
                dy = abs(hook_pos[1] - self.last_hook_pos[1])
                stable_pos = (dx <= 20 and dy <= 10)

            self.green_confirm_count = self.green_confirm_count + 1 if stable_pos else 1
            self.last_hook_pos = hook_pos
        else:
            self.green_confirm_count = 0
            self.last_hook_pos = None

        high_confidence = best_in_green and best_green_ratio >= 0.38 and (14 <= bar_width <= 220)
        can_click = (self.green_confirm_count >= self.cfg.green_confirm_frames) or high_confidence

        status = (
            f"Bar width={bar_width:3d} | green={best_green_ratio:6.2%} | "
            f"confirm={self.green_confirm_count}/{self.cfg.green_confirm_frames}"
        )
        print(status + "      ", end="\r")

        if not can_click:
            return

        pyautogui.click(click_pos)
        self._park_cursor_in_corner()
        self._save_debug_frame(frame, "hook_click")
        time.sleep(self.cfg.post_click_delay_sec)
        self._transition_to_wait_scale()

    def _process_wait_scale(self, frame: np.ndarray) -> None:
        elapsed = time.time() - self.scale_wait_start
        if elapsed > self.cfg.wait_scale_timeout_sec:
            self.logger.info("Scale wait timeout, restarting cycle")
            self._save_debug_frame(frame, "scale_wait_timeout")
            self._finalize_cycle(False, "wait_timeout")
            self._transition_to_search()
            return

        if elapsed < self.cfg.wait_scale_warmup_sec:
            self.scale_confirm_count = 0
            self.last_scale_candidate = None
            self._debug_wait_scale_log(elapsed, None, None, False, "warmup")
            print(f"Waiting scale warmup... {elapsed:4.1f}s      ", end="\r")
            return

        if elapsed < self.cfg.wait_scale_min_accept_sec:
            self.scale_confirm_count = 0
            self.last_scale_candidate = None
            self._debug_wait_scale_log(elapsed, None, None, False, "min_accept_delay")
            print(f"Waiting scale lock... {elapsed:4.1f}s      ", end="\r")
            return

        scale = self._find_vertical_scale(frame)
        if scale is None:
            self.scale_confirm_count = 0
            self.last_scale_candidate = None
            self._debug_wait_scale_log(elapsed, None, None, False, "scale_not_found")
            print(f"Waiting scale... {int(elapsed)}s      ", end="\r")
            return

        topbar_score = self._score_minigame_top_bar(frame, scale["x"])
        now_ts = time.time()
        if topbar_score >= self.cfg.ui_topbar_score_accept:
            self.topbar_confirm_count += 1
        else:
            self.topbar_confirm_count = max(0, self.topbar_confirm_count - 1)

        if topbar_score >= self.cfg.ui_topbar_score_strict:
            self.topbar_seen_until_ts = now_ts + self.cfg.ui_topbar_seen_hold_sec
        topbar_latched = (self.topbar_confirm_count >= self.cfg.ui_topbar_confirm_frames) or (now_ts <= self.topbar_seen_until_ts)

        float_pos, _ = self._find_float_on_scale(frame, scale)
        float_confirmed = (
            (float_pos is not None)
            and (self.cfg.scale_min_float_pos <= float_pos <= self.cfg.scale_max_float_pos)
        )

        stable_ok = self._is_stable_scale(scale)
        scale_visible_ratio = float(np.mean(self.detector.green_mask(frame[max(0, scale['top_y']):min(frame.shape[0], scale['top_y'] + scale['height'] + 1), max(0, scale['x'] - 5):min(frame.shape[1], scale['x'] + 6)])))
        fallback_ok = (
            stable_ok
            and (scale["height"] >= self.cfg.fallback_scale_min_height_px)
            and (float_pos is not None)
            and (self.cfg.fallback_float_min_pos <= float_pos <= self.cfg.fallback_float_max_pos)
            and (scale_visible_ratio >= self.cfg.fallback_scale_min_visible_ratio)
        )

        ui_gate_ok = topbar_latched or fallback_ok
        if ui_gate_ok and float_confirmed and stable_ok and scale_visible_ratio >= self.cfg.scale_visible_ratio_min:
            self.scale_confirm_count += 1
            self._debug_wait_scale_log(elapsed, scale, float_pos, stable_ok, "candidate_ok")
        else:
            self.scale_confirm_count = 0
            if not ui_gate_ok:
                reason = f"topbar_weak_{topbar_score:.2f}"
            elif not float_confirmed:
                reason = "float_out_of_range"
            elif not stable_ok:
                reason = "unstable_scale"
            else:
                reason = "weak_scale_signal"
            self._debug_wait_scale_log(elapsed, scale, float_pos, stable_ok, reason)
            self._debug_wait_scale_snapshot(frame, scale, reason)
            if not float_confirmed:
                self.last_scale_candidate = None

        print(
            f"Scale h={scale['height']} | ui={topbar_score:.2f} | float={'yes' if float_confirmed else 'no'} | "
            f"confirm={self.scale_confirm_count}/{self.cfg.scale_confirm_frames}      ",
            end="\r",
        )

        if self.scale_confirm_count >= self.cfg.scale_confirm_frames:
            self._save_debug_frame(frame, "scale_confirmed")
            self._transition_to_play_scale()

    def _process_play_scale(self, frame: np.ndarray) -> None:
        elapsed = time.time() - self.play_scale_start
        if elapsed > self.cfg.play_scale_timeout_sec:
            self.logger.info("Scale play timeout, cycle finished")
            self._save_debug_frame(frame, "scale_play_timeout")
            self._finalize_cycle(False, "play_timeout")
            self._transition_to_search()
            time.sleep(self.cfg.post_cycle_cooldown_sec)
            return

        scale = self._find_vertical_scale(frame)
        if scale is None:
            self.scale_lost_count += 1
            if self.scale_lost_count >= self.cfg.lost_scale_frames:
                self.logger.info("Scale disappeared, cycle finished with %d clicks", self.clicks_count)
                self._save_debug_frame(frame, "scale_disappeared")
                self._finalize_cycle(True, "scale_disappeared")
                self._transition_to_search()
                time.sleep(self.cfg.post_cycle_cooldown_sec)
            return

        self.scale_lost_count = 0
        float_pos, zone = self._find_float_on_scale(frame, scale)
        now_ts = time.perf_counter()

        if float_pos is None:
            self.play_float_ema = None
            self.play_last_float_raw = None
            if (now_ts - self.play_last_click_ts) >= self.cfg.play_lost_float_safe_click_interval_sec:
                pyautogui.click()
                self.play_last_click_ts = now_ts
                self.clicks_count += 1
                print(f"Float lost, safe click #{self.clicks_count}      ", end="\r")
            else:
                print("Float lost, waiting safe interval      ", end="\r")
            return

        click_now, ema, predicted, lower, upper, interval_sec = self._play_click_controller(float_pos, now_ts)

        if click_now:
            pyautogui.click()
            self.play_last_click_ts = now_ts
            self.clicks_count += 1
            action = f"click #{self.clicks_count}"
        else:
            action = "wait"

        print(
            f"Float raw={float_pos:.2f} ema={ema:.2f} pred={predicted:.2f} {zone} | "
            f"band={lower:.2f}-{upper:.2f} | {action} | dt={int(interval_sec * 1000)}ms      ",
            end="\r",
        )

    def run(self) -> None:
        print("=" * 56)
        print("Fishing Bot v3")
        print("7/num7 start/pause | 8/num8/esc exit | 0/num0 debug")
        print("=" * 56)

        self._register_hotkeys()

        def poll_keys_fallback() -> None:
            if keyboard.is_pressed("8") or keyboard.is_pressed("num 8") or keyboard.is_pressed("esc"):
                self._on_hotkey_exit()
            if keyboard.is_pressed("7") or keyboard.is_pressed("num 7"):
                self._on_hotkey_toggle_active()
            if keyboard.is_pressed("0") or keyboard.is_pressed("num 0"):
                self._on_hotkey_toggle_debug()

        try:
            with mss.mss() as sct:
                while True:
                    if not self.hotkeys_registered:
                        poll_keys_fallback()

                    if self.hotkey_exit_requested:
                        print("\nExit")
                        break

                    if self.hotkey_toggle_active_requested:
                        self.hotkey_toggle_active_requested = False
                        self.active = not self.active
                        print(f"\nStatus: {'ACTIVE' if self.active else 'PAUSE'}")

                    if self.hotkey_toggle_debug_requested:
                        self.hotkey_toggle_debug_requested = False
                        self.debug_mode = not self.debug_mode
                        print(f"\nDebug: {'ON' if self.debug_mode else 'OFF'}")

                    if not self.active:
                        self.limiters["inactive"].wait()
                        continue

                    frame = self._capture_screen(sct)
                    self.detector.adapt(frame)

                    try:
                        if self.state == BotState.SEARCH_BAR:
                            self._process_search_bar(frame)
                        elif self.state == BotState.WAIT_SCALE:
                            self._process_wait_scale(frame)
                        elif self.state == BotState.PLAY_SCALE:
                            self._process_play_scale(frame)
                    except Exception as exc:
                        self.logger.exception("State %s failed: %s", self.state.name, exc)
                        self._save_debug_frame(frame, f"error_{self.state.name.lower()}")
                        self._transition_to_search()

                    self.limiters[self.state].wait()
        finally:
            self._unregister_hotkeys()


if __name__ == "__main__":
    bot = FishingBotV3()
    bot.run()
