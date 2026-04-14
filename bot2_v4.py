import importlib
import logging
import os
import queue
import re
import shutil
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional, Tuple

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

    ocr_enabled: bool = True
    ocr_interval_sec: float = 1.05
    ocr_lang: str = "rus+eng"
    ocr_psm: int = 6
    ocr_roi_x_ratio: float = 0.0
    ocr_roi_y_ratio: float = 0.24
    ocr_roi_w_ratio: float = 0.24
    ocr_roi_h_ratio: float = 0.33
    ocr_min_line_len: int = 8
    ocr_seen_ttl_sec: float = 2.4
    ocr_event_ttl_sec: float = 14.0
    ocr_fish_seen_ttl_sec: float = 7.5
    ocr_cross_cycle_guard_sec: float = 2.6
    ocr_cast_fail_restart_cooldown_sec: float = 8.0
    ocr_seen_max_entries: int = 220
    ocr_tesseract_cmd: str = ""
    ocr_tessdata_dir: str = "tessdata"
    ocr_error_log_cooldown_sec: float = 4.0
    ocr_no_match_log_interval_sec: float = 3.0
    ocr_use_worker: bool = True
    ocr_auto_roi: bool = False
    ocr_auto_scan_x_ratio: float = 0.12
    ocr_auto_scan_y_top_ratio: float = 0.18
    ocr_auto_scan_y_bottom_ratio: float = 0.80
    ocr_auto_target_w_ratio: float = 0.34

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
        self.ocr_last_ts = 0.0
        self.ocr_seen_lines = {}
        self.ocr_seen_events = {}
        self.ocr_seen_fish = {}
        self.ocr_seen_fish_exp = {}
        self.ocr_total_exp = 0
        self.ocr_fish_counter: Counter[str] = Counter()
        self.pytesseract_module = self._load_pytesseract()
        self.ocr_available = self.pytesseract_module is not None
        self.ocr_init_error: Optional[str] = None
        self.ocr_last_error_log_ts = 0.0
        self.ocr_last_no_match_log_ts = 0.0
        self.ocr_last_cast_fail_ts = 0.0
        self.ocr_request_queue: queue.Queue = queue.Queue(maxsize=1)
        self.ocr_result_queue: queue.Queue = queue.Queue(maxsize=2)
        self.ocr_worker_stop_event = threading.Event()
        self.ocr_worker_thread: Optional[threading.Thread] = None

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
        self._configure_ocr_backend()

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
            self.logger.info("Hotkeys ready: 7/num7 start-stop, 8/num8 exit, 0/num0 debug")
        else:
            self.logger.warning("Hotkeys unavailable, fallback to key polling")

        if self.cfg.ocr_enabled:
            if self.ocr_available:
                cmd = getattr(getattr(self.pytesseract_module, "pytesseract", None), "tesseract_cmd", "unknown")
                self.logger.info("OCR notifications enabled (tesseract: %s)", cmd)
                self._start_ocr_worker()
            else:
                self.logger.warning(
                    "OCR notifications disabled: %s",
                    self.ocr_init_error or "pytesseract or tesseract is not available",
                )

    def _unregister_hotkeys(self) -> None:
        self._stop_ocr_worker()
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

    @staticmethod
    def _load_pytesseract() -> Optional[Any]:
        try:
            return importlib.import_module("pytesseract")
        except Exception:
            return None

    def _resolve_tesseract_cmd(self) -> Optional[str]:
        candidates = []

        cfg_cmd = self.cfg.ocr_tesseract_cmd.strip()
        if cfg_cmd:
            candidates.append(cfg_cmd)

        env_cmd = os.environ.get("TESSERACT_CMD", "").strip()
        if env_cmd:
            candidates.append(env_cmd)

        which_cmd = shutil.which("tesseract")
        if which_cmd:
            candidates.append(which_cmd)

        candidates.extend(
            [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        )

        for candidate in candidates:
            candidate_path = Path(candidate)
            if candidate_path.is_file():
                return str(candidate_path)
        return None

    def _resolve_tessdata_prefix(self) -> Optional[str]:
        env_prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
        if env_prefix and Path(env_prefix).is_dir():
            return env_prefix

        candidates = []
        cfg_tessdata = self.cfg.ocr_tessdata_dir.strip()
        if cfg_tessdata:
            cfg_path = Path(cfg_tessdata)
            candidates.append(cfg_path if cfg_path.is_absolute() else (Path.cwd() / cfg_path))

        candidates.append(Path(__file__).resolve().parent / "tessdata")

        for candidate in candidates:
            if candidate.is_dir() and (candidate / "eng.traineddata").exists():
                return str(candidate)
        return None

    def _configure_ocr_backend(self) -> None:
        if self.pytesseract_module is None:
            self.ocr_available = False
            self.ocr_init_error = "pytesseract package is not installed"
            return

        cmd = self._resolve_tesseract_cmd()
        if cmd is None:
            self.ocr_available = False
            self.ocr_init_error = "tesseract.exe not found in PATH or standard directories"
            return

        self.pytesseract_module.pytesseract.tesseract_cmd = cmd

        tessdata_prefix = self._resolve_tessdata_prefix()
        if tessdata_prefix is not None:
            os.environ["TESSDATA_PREFIX"] = tessdata_prefix

        try:
            self.pytesseract_module.get_tesseract_version()
        except Exception as exc:
            self.ocr_available = False
            self.ocr_init_error = str(exc)
            return

        self.ocr_available = True
        self.ocr_init_error = None

    def _log_ocr_warning(self, message: str, *args: object) -> None:
        now = time.time()
        if now - self.ocr_last_error_log_ts < max(0.2, self.cfg.ocr_error_log_cooldown_sec):
            return
        self.ocr_last_error_log_ts = now
        self.logger.warning(message, *args)

    @staticmethod
    def _text_has_notification_hint(normalized_text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:улов|вытян|вылов|поймал|получ|опыт|приманк|рыб|fish|xp|exp)\b",
                normalized_text,
            )
        )

    @staticmethod
    def _text_has_fish_anchor(normalized_text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:улов|вытян\w*|вылов\w*|поймал\w*|рыба|fish|caught)\b",
                normalized_text,
            )
        )

    @staticmethod
    def _text_has_cast_fail_hint(normalized_text: str) -> bool:
        has_cast = bool(re.search(r"\bзаброс\w*\b", normalized_text))
        has_rod = bool(re.search(r"\bудочк\w*\b", normalized_text))
        has_fail = bool(re.search(r"\b(?:провал\w*|неудач\w*|сорва\w*|не\s+удал\w*|failed?)\b", normalized_text))
        has_retry = bool(re.search(r"\b(?:еще\s+раз|ещ[её]\s+раз|try\s+again)\b", normalized_text))
        return has_cast and has_rod and has_fail and has_retry

    def _restart_cycle_from_cast_fail_if_needed(self, normalized_text: str, frame_bgr: np.ndarray) -> bool:
        if self.state not in (BotState.WAIT_SCALE, BotState.PLAY_SCALE):
            return False

        if not self._text_has_cast_fail_hint(normalized_text):
            return False

        now_ts = time.time()
        cooldown = max(1.0, self.cfg.ocr_cast_fail_restart_cooldown_sec)
        if now_ts - self.ocr_last_cast_fail_ts < cooldown:
            return True

        self.ocr_last_cast_fail_ts = now_ts
        self.logger.info("OCR detected failed cast notification, restarting cycle")
        if self.debug_mode:
            self._save_debug_frame(frame_bgr, "cast_failed_notice")
        self._finalize_cycle(False, "cast_failed")
        self._transition_to_search()
        return True

    def _extract_notification_regions(self, roi_bgr: np.ndarray) -> list[np.ndarray]:
        roi_h, roi_w = roi_bgr.shape[:2]
        if roi_h < 28 or roi_w < 90:
            return [roi_bgr]

        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # Notification cards are dark and mostly low-saturation blocks on the left.
        dark_neutral = (sat <= 95) & (val >= 18) & (val <= 160)
        mask = dark_neutral.astype(np.uint8) * 255

        close_kernel = np.ones((5, 7), np.uint8)
        open_kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, open_kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []

        min_w = max(110, int(roi_w * 0.30))
        max_h = max(52, int(roi_h * 0.34))
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if w < min_w or h < 22 or h > max_h:
                continue
            if x > int(roi_w * 0.26):
                continue
            if (w * h) < 2200:
                continue

            block = roi_bgr[y:y + h, x:x + w]
            if block.size == 0:
                continue

            gray = cv2.cvtColor(block, cv2.COLOR_BGR2GRAY)
            bright_ratio = float(np.mean(gray >= 145))
            if bright_ratio < 0.010 or bright_ratio > 0.38:
                continue

            boxes.append((x, y, w, h))

        if not boxes:
            return [roi_bgr]

        boxes.sort(key=lambda b: b[1])
        merged = []
        for x, y, w, h in boxes:
            if not merged:
                merged.append([x, y, w, h])
                continue

            mx, my, mw, mh = merged[-1]
            m_bottom = my + mh
            if y <= m_bottom + 8:
                nx0 = min(mx, x)
                ny0 = min(my, y)
                nx1 = max(mx + mw, x + w)
                ny1 = max(m_bottom, y + h)
                merged[-1] = [nx0, ny0, nx1 - nx0, ny1 - ny0]
            else:
                merged.append([x, y, w, h])

        regions = []
        for x, y, w, h in merged[-5:]:
            x0 = max(0, x + 16)
            x1 = min(roi_w, x + w - 8)
            y0 = max(0, y + 2)
            y1 = min(roi_h, y + h - 2)
            if (x1 - x0) < 40 or (y1 - y0) < 14:
                continue
            regions.append(roi_bgr[y0:y1, x0:x1])

        return regions if regions else [roi_bgr]

    def _ocr_read_roi(self, roi_bgr: np.ndarray) -> str:
        regions = self._extract_notification_regions(roi_bgr)
        prepared = []

        for region in regions:
            pre = self._ocr_preprocess(region)
            if pre.size > 0:
                prepared.append(pre)

        if not prepared:
            prepared = [self._ocr_preprocess(roi_bgr)]

        if len(prepared) == 1:
            ocr_target = prepared[0]
        else:
            separator = 12
            max_w = max(img.shape[1] for img in prepared)
            total_h = sum(img.shape[0] for img in prepared) + separator * (len(prepared) - 1)
            ocr_target = np.full((total_h, max_w), 255, dtype=np.uint8)

            y = 0
            for img in prepared:
                h, w = img.shape[:2]
                ocr_target[y:y + h, 0:w] = img
                y += h + separator

        ocr_config = f"--oem 3 --psm {int(self.cfg.ocr_psm)} -c preserve_interword_spaces=1"
        return self.pytesseract_module.image_to_string(ocr_target, lang=self.cfg.ocr_lang, config=ocr_config)

    def _start_ocr_worker(self) -> None:
        if not self.cfg.ocr_use_worker or not self.ocr_available:
            return
        if self.ocr_worker_thread is not None and self.ocr_worker_thread.is_alive():
            return

        self.ocr_worker_stop_event.clear()
        self.ocr_worker_thread = threading.Thread(
            target=self._ocr_worker_loop,
            name="ocr_worker",
            daemon=True,
        )
        self.ocr_worker_thread.start()

    def _stop_ocr_worker(self) -> None:
        self.ocr_worker_stop_event.set()

        if self.ocr_worker_thread is not None and self.ocr_worker_thread.is_alive():
            try:
                self.ocr_request_queue.put_nowait(None)
            except queue.Full:
                pass
            self.ocr_worker_thread.join(timeout=0.8)

        self.ocr_worker_thread = None

        while True:
            try:
                self.ocr_request_queue.get_nowait()
            except queue.Empty:
                break

        while True:
            try:
                self.ocr_result_queue.get_nowait()
            except queue.Empty:
                break

    def _ocr_worker_loop(self) -> None:
        while not self.ocr_worker_stop_event.is_set():
            try:
                payload = self.ocr_request_queue.get(timeout=0.20)
            except queue.Empty:
                continue

            if payload is None:
                continue

            req_ts, roi_bgr, roi_box = payload
            raw_text = ""
            error_text = None

            try:
                raw_text = self._ocr_read_roi(roi_bgr)
            except Exception as exc:
                error_text = str(exc)

            result = {
                "request_ts": req_ts,
                "roi_box": roi_box,
                "raw_text": raw_text,
                "error": error_text,
            }

            try:
                self.ocr_result_queue.put_nowait(result)
            except queue.Full:
                try:
                    self.ocr_result_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.ocr_result_queue.put_nowait(result)
                except queue.Full:
                    pass

    def _drain_ocr_results(self, frame_bgr: np.ndarray) -> None:
        while True:
            try:
                result = self.ocr_result_queue.get_nowait()
            except queue.Empty:
                break

            self._handle_ocr_text_result(
                raw_text=result.get("raw_text", ""),
                error_text=result.get("error"),
                roi_box=result.get("roi_box"),
                frame_bgr=frame_bgr,
            )

    def _log_ocr_no_match(self, raw_text: str) -> None:
        if not self.debug_mode:
            return

        now = time.time()
        if now - self.ocr_last_no_match_log_ts < max(0.3, self.cfg.ocr_no_match_log_interval_sec):
            return
        self.ocr_last_no_match_log_ts = now

        compact = " ".join([line.strip() for line in raw_text.splitlines() if line.strip()])
        compact = compact[:180]
        normalized = self._normalize_ocr_text(compact)
        if compact and self._text_has_notification_hint(normalized):
            self.logger.info("[OCR-DBG] no parse match: %s", compact)

    def _handle_ocr_text_result(
        self,
        raw_text: str,
        error_text: Optional[str],
        roi_box: Optional[Tuple[int, int, int, int]],
        frame_bgr: np.ndarray,
    ) -> None:
        if error_text:
            msg_l = error_text.lower()
            if "tesseract is not installed" in msg_l or "not in your path" in msg_l:
                self._configure_ocr_backend()
                if self.ocr_available:
                    self._start_ocr_worker()
                else:
                    self._log_ocr_warning("OCR disabled: %s", self.ocr_init_error or error_text)
                return

            self._log_ocr_warning("OCR read failed: %s", error_text)
            return

        if not raw_text:
            return

        normalized_raw = self._normalize_ocr_text(raw_text)
        if self._restart_cycle_from_cast_fail_if_needed(normalized_raw, frame_bgr):
            return

        now_ts = time.time()
        self._cleanup_seen_ocr_lines(now_ts)

        lines = [line.strip() for line in raw_text.splitlines()]
        lines = [line for line in lines if len(line) >= self.cfg.ocr_min_line_len]
        if not lines:
            return

        candidates = []
        max_candidate_len = 180
        merged = " ".join(lines)
        if self.cfg.ocr_min_line_len <= len(merged) <= max_candidate_len:
            candidates.append(merged)
        for i in range(len(lines) - 1):
            pair = f"{lines[i]} {lines[i + 1]}"
            if len(pair) <= max_candidate_len:
                candidates.append(pair)
        candidates.extend([line for line in lines if len(line) <= max_candidate_len])

        local_seen = set()
        local_events = set()

        for raw_line in candidates:
            normalized = self._normalize_ocr_text(raw_line)
            if len(normalized) < self.cfg.ocr_min_line_len or normalized in local_seen:
                continue
            if not self._text_has_notification_hint(normalized):
                continue
            local_seen.add(normalized)

            prev_ts = self.ocr_seen_lines.get(normalized)
            if prev_ts is not None and (now_ts - prev_ts) < self.cfg.ocr_seen_ttl_sec:
                continue

            fish_name, exp = self._parse_notification_line(raw_line)
            if fish_name is None and exp is None:
                continue

            fish_key = self._normalize_ocr_text(fish_name) if fish_name is not None else "-"
            event_key = f"{fish_key}|{exp if exp is not None else '-'}"

            prev_event = self.ocr_seen_events.get(event_key)
            if prev_event is not None:
                if isinstance(prev_event, tuple):
                    prev_event_ts = float(prev_event[0])
                    prev_event_cycle = int(prev_event[1])
                else:
                    prev_event_ts = float(prev_event)
                    prev_event_cycle = self.cycles_total

                dt_event = now_ts - prev_event_ts
                if prev_event_cycle == self.cycles_total:
                    if dt_event < max(2.0, self.cfg.ocr_event_ttl_sec):
                        continue
                else:
                    if dt_event < max(0.8, self.cfg.ocr_cross_cycle_guard_sec):
                        continue

            if event_key in local_events:
                continue
            local_events.add(event_key)
            self.ocr_seen_events[event_key] = (now_ts, self.cycles_total)

            self.ocr_seen_lines[normalized] = now_ts

            if fish_name is not None:
                prev_fish_entry = self.ocr_seen_fish.get(fish_key)
                if prev_fish_entry is None:
                    prev_fish_ts = None
                    prev_fish_cycle = self.cycles_total
                elif isinstance(prev_fish_entry, tuple):
                    prev_fish_ts = float(prev_fish_entry[0])
                    prev_fish_cycle = int(prev_fish_entry[1])
                else:
                    prev_fish_ts = float(prev_fish_entry)
                    prev_fish_cycle = self.cycles_total

                fish_seen_recently = False
                if prev_fish_ts is not None:
                    dt_fish = now_ts - prev_fish_ts
                    if prev_fish_cycle == self.cycles_total:
                        fish_seen_recently = dt_fish < max(1.0, self.cfg.ocr_fish_seen_ttl_sec)
                    else:
                        fish_seen_recently = dt_fish < max(0.8, self.cfg.ocr_cross_cycle_guard_sec)

                prev_exp = self.ocr_seen_fish_exp.get(fish_key)

                if not fish_seen_recently:
                    if exp is None:
                        # Keep tentative event, wait for XP to avoid noisy false positives.
                        self.ocr_seen_fish[fish_key] = (now_ts, self.cycles_total)
                        self.ocr_seen_fish_exp[fish_key] = None
                        continue

                    self.ocr_fish_counter[fish_name] += 1
                    self.ocr_total_exp += exp
                    self.ocr_seen_fish[fish_key] = (now_ts, self.cycles_total)
                    self.ocr_seen_fish_exp[fish_key] = exp
                else:
                    self.ocr_seen_fish[fish_key] = (now_ts, self.cycles_total)

                    if exp is None:
                        continue

                    if prev_exp is None:
                        # First reliable XP for a tentative fish event.
                        self.ocr_fish_counter[fish_name] += 1
                        self.ocr_total_exp += exp
                        self.ocr_seen_fish_exp[fish_key] = exp
                    elif exp > prev_exp:
                        # OCR corrected XP upward for the same card.
                        self.ocr_total_exp += (exp - prev_exp)
                        self.ocr_seen_fish_exp[fish_key] = exp
                    else:
                        # Same or worse OCR read for the same card.
                        continue
            elif exp is not None:
                self.ocr_total_exp += exp

            fish_txt = fish_name if fish_name is not None else "-"
            exp_txt = str(exp) if exp is not None else "-"

            rx0, ry0, rx1, ry1 = (roi_box if roi_box is not None else (0, 0, 0, 0))
            print(
                f"\n[OCR] Fish: {fish_txt} | XP: {exp_txt} | total_xp={self.ocr_total_exp}",
                end="\n",
            )
            self.logger.info(
                "OCR notice: fish=%s exp=%s total_xp=%d roi=(%d,%d)-(%d,%d)",
                fish_txt,
                exp_txt,
                self.ocr_total_exp,
                rx0,
                ry0,
                rx1,
                ry1,
            )

            if self.debug_mode and roi_box is not None:
                debug = frame_bgr.copy()
                cv2.rectangle(debug, (rx0, ry0), (rx1, ry1), (255, 200, 0), 2)
                cv2.putText(
                    debug,
                    "OCR ROI",
                    (rx0 + 6, max(20, ry0 + 22)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 200, 0),
                    2,
                )
                self._save_debug_frame(debug, "ocr_notice")

        if not local_events:
            self._log_ocr_no_match(raw_text)

    @staticmethod
    def _normalize_ocr_text(text: str) -> str:
        text = text.lower()
        text = text.replace("ё", "е")
        text = re.sub(r"[^0-9a-zа-я\s:\-\+']", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _token_has_cyrillic(token: str) -> bool:
        return bool(re.search(r"[а-я]", token))

    def _normalize_fish_name(self, fish_raw: str) -> Optional[str]:
        fish = self._normalize_ocr_text(fish_raw)
        if not fish:
            return None

        fish = re.sub(r"\b(?:и|а)\s+получ\w*.*$", "", fish)
        fish = re.sub(r"\b(?:получ\w*|gained|earned)\b.*$", "", fish)
        fish = re.sub(r"(?:\+\s*)?\d{1,6}\s*(?:xp|exp|опыт\w*)\b.*$", "", fish)

        skip_leading_tokens = {
            "вы",
            "you",
            "улов",
            "рыба",
            "fish",
            "вытянул",
            "вытянула",
            "вытянули",
            "выловил",
            "выловила",
            "выловили",
            "поймал",
            "поймала",
            "поймали",
            "caught",
            "получено",
            "получил",
            "получила",
            "получили",
            "опыт",
            "опыта",
            "xp",
            "exp",
        }

        boundary_tokens = {
            "со",
            "с",
            "в",
            "на",
            "по",
            "к",
            "у",
            "из",
            "для",
            "забросьте",
            "удочку",
            "ожидание",
            "клева",
            "установлена",
            "походка",
            "нормальная",
            "нажмите",
            "клавишу",
            "приманка",
            "приманку",
            "press",
            "cast",
            "rod",
            "waiting",
            "bite",
        }

        tokens = []
        saw_cyrillic = False
        for token in fish.split():
            if token in skip_leading_tokens:
                if tokens:
                    break
                continue

            if token in boundary_tokens:
                if tokens:
                    break
                continue

            if any(ch.isdigit() for ch in token):
                if tokens:
                    break
                continue

            if len(token) < 2:
                if tokens:
                    break
                continue

            has_cyr = self._token_has_cyrillic(token)
            if tokens and saw_cyrillic and not has_cyr:
                break

            if len(tokens) >= 3:
                break

            tokens.append(token)
            saw_cyrillic = saw_cyrillic or has_cyr

        while len(tokens) > 1 and len(tokens[-1]) <= 2:
            tokens.pop()

        if not tokens:
            return None

        candidate = " ".join(tokens).strip(" -:")
        if len(candidate) < 3:
            return None
        if not re.search(r"[a-zа-я]", candidate):
            return None

        return " ".join(token.capitalize() for token in candidate.split())

    def _parse_notification_line(self, line: str) -> Tuple[Optional[str], Optional[int]]:
        normalized = self._normalize_ocr_text(line)
        if not normalized:
            return None, None

        exp = None
        xp_token = r"(?:xp|xр|хр|exp|опыт(?:а)?)"
        exp_patterns = [
            rf"(?:\+\s*)?(\d{{1,6}})\s*{xp_token}\b",
            rf"\b{xp_token}\s*[:=\-\+ ]\s*(\d{{1,6}})\b",
            r"\b(?:получ(?:ено|ил|ила|или)|gained|earned)\s*[:=\-\+ ]?\s*(\d{1,6})\b",
        ]
        for pattern in exp_patterns:
            match = re.search(pattern, normalized)
            if match:
                exp = int(match.group(1))
                if exp is not None:
                    break

        fish_name = None
        fish_patterns = [
            r"\b(?:вытянул|вытянула|вытянули)\s+улов\s*[:\-]?\s*([a-zа-я][a-zа-я\-']{1,24}(?:\s+[a-zа-я][a-zа-я\-']{1,24}){0,4})",
            r"\bулов\s*[:\-]?\s*([a-zа-я][a-zа-я\-']{1,24}(?:\s+[a-zа-я][a-zа-я\-']{1,24}){0,4})",
            r"\b(?:вы\s+)?(?:выловил|выловила|выловили|поймал|поймала|поймали|caught)\s+([a-zа-я][a-zа-я\-']{1,24}(?:\s+[a-zа-я][a-zа-я\-']{1,24}){0,4})",
            r"\b(?:рыба|fish)\s*[:\-]?\s*([a-zа-я][a-zа-я\-']{1,24}(?:\s+[a-zа-я][a-zа-я\-']{1,24}){0,4})",
        ]
        for pattern in fish_patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            candidate = self._normalize_fish_name(match.group(1))
            if candidate is not None:
                fish_name = candidate
                break

        if fish_name is None and self._text_has_fish_anchor(normalized):
            fish_name = self._normalize_fish_name(normalized)

        return fish_name, exp

    def _estimate_notification_roi(self, frame_bgr: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        if not self.cfg.ocr_auto_roi:
            return None

        h, w = frame_bgr.shape[:2]
        sx0 = 0
        sx1 = max(30, int(w * self.cfg.ocr_auto_scan_x_ratio))
        sy0 = int(np.clip(self.cfg.ocr_auto_scan_y_top_ratio, 0.0, 0.95) * h)
        sy1 = int(np.clip(self.cfg.ocr_auto_scan_y_bottom_ratio, 0.05, 1.0) * h)
        sy1 = max(sy0 + 12, sy1)

        scan = frame_bgr[sy0:sy1, sx0:sx1]
        if scan.size == 0:
            return None

        hsv = cv2.cvtColor(scan, cv2.COLOR_BGR2HSV)
        hh = hsv[:, :, 0]
        ss = hsv[:, :, 1]
        vv = hsv[:, :, 2]

        color_mask = (
            ((hh >= 25) & (hh <= 105) & (ss >= 55) & (vv >= 70))
            | ((ss <= 70) & (vv >= 175))
        )

        mask_u8 = color_mask.astype(np.uint8) * 255
        kernel = np.ones((3, 3), np.uint8)
        mask_u8 = cv2.morphologyEx(mask_u8, cv2.MORPH_OPEN, kernel)

        row_strength = np.mean(mask_u8 > 0, axis=1)
        rows = np.where(row_strength >= 0.09)[0]
        if rows.size < 8:
            return None

        runs = []
        run_start = int(rows[0])
        prev = int(rows[0])
        for r in rows[1:]:
            r_int = int(r)
            if r_int <= prev + 8:
                prev = r_int
                continue
            if prev - run_start + 1 >= 4:
                runs.append((run_start, prev))
            run_start = r_int
            prev = r_int
        if prev - run_start + 1 >= 4:
            runs.append((run_start, prev))

        if not runs:
            return None

        top_rel = runs[0][0]
        bottom_rel = runs[-1][1]

        ay0 = max(0, sy0 + top_rel - 24)
        ay1 = min(h, sy0 + bottom_rel + 28)
        min_h = max(60, int(h * 0.16))
        if ay1 - ay0 < min_h:
            pad = (min_h - (ay1 - ay0)) // 2 + 6
            ay0 = max(0, ay0 - pad)
            ay1 = min(h, ay1 + pad)

        ax0 = 0
        ax1 = min(w, int(w * np.clip(self.cfg.ocr_auto_target_w_ratio, 0.12, 0.60)))

        if ay1 <= ay0 or ax1 <= ax0:
            return None
        return int(ax0), int(ay0), int(ax1), int(ay1)

    def _get_notification_roi(self, frame_bgr: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int, int, int]]:
        auto_box = self._estimate_notification_roi(frame_bgr)
        if auto_box is not None:
            x0, y0, x1, y1 = auto_box
            return frame_bgr[y0:y1, x0:x1], auto_box

        h, w = frame_bgr.shape[:2]
        x0 = int(np.clip(self.cfg.ocr_roi_x_ratio, 0.0, 0.95) * w)
        y0 = int(np.clip(self.cfg.ocr_roi_y_ratio, 0.0, 0.95) * h)
        ww = int(np.clip(self.cfg.ocr_roi_w_ratio, 0.05, 1.0) * w)
        hh = int(np.clip(self.cfg.ocr_roi_h_ratio, 0.05, 1.0) * h)
        x1 = min(w, x0 + ww)
        y1 = min(h, y0 + hh)

        if x1 <= x0 or y1 <= y0:
            return frame_bgr, (0, 0, w, h)

        return frame_bgr[y0:y1, x0:x1], (x0, y0, x1, y1)

    def _ocr_preprocess(self, roi_bgr: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        upscaled = cv2.resize(gray, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
        upscaled = cv2.GaussianBlur(upscaled, (3, 3), 0)

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(upscaled)

        _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            enhanced,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            35,
            7,
        )

        merged = cv2.bitwise_and(otsu, adaptive)
        merged = cv2.morphologyEx(merged, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

        # Keep OCR target in black text over white background.
        if float(np.mean(merged)) < 120.0:
            merged = cv2.bitwise_not(merged)

        return merged

    def _cleanup_seen_ocr_lines(self, now_ts: float) -> None:
        ttl = max(1.0, self.cfg.ocr_seen_ttl_sec)
        stale = [key for key, ts in self.ocr_seen_lines.items() if now_ts - ts > ttl]
        for key in stale:
            self.ocr_seen_lines.pop(key, None)

        if len(self.ocr_seen_lines) > self.cfg.ocr_seen_max_entries:
            overflow = len(self.ocr_seen_lines) - self.cfg.ocr_seen_max_entries
            for key, _ in sorted(self.ocr_seen_lines.items(), key=lambda kv: kv[1])[:overflow]:
                self.ocr_seen_lines.pop(key, None)

        event_ttl = max(2.0, self.cfg.ocr_event_ttl_sec)
        stale_events = []
        for key, value in self.ocr_seen_events.items():
            ts = float(value[0]) if isinstance(value, tuple) else float(value)
            if now_ts - ts > event_ttl:
                stale_events.append(key)
        for key in stale_events:
            self.ocr_seen_events.pop(key, None)

        if len(self.ocr_seen_events) > self.cfg.ocr_seen_max_entries:
            overflow = len(self.ocr_seen_events) - self.cfg.ocr_seen_max_entries
            for key, _ in sorted(
                self.ocr_seen_events.items(),
                key=lambda kv: (float(kv[1][0]) if isinstance(kv[1], tuple) else float(kv[1])),
            )[:overflow]:
                self.ocr_seen_events.pop(key, None)

        fish_ttl = max(1.0, self.cfg.ocr_fish_seen_ttl_sec)
        stale_fish = []
        for key, value in self.ocr_seen_fish.items():
            ts = float(value[0]) if isinstance(value, tuple) else float(value)
            if now_ts - ts > fish_ttl:
                stale_fish.append(key)
        for key in stale_fish:
            self.ocr_seen_fish.pop(key, None)

        if len(self.ocr_seen_fish) > self.cfg.ocr_seen_max_entries:
            overflow = len(self.ocr_seen_fish) - self.cfg.ocr_seen_max_entries
            for key, _ in sorted(
                self.ocr_seen_fish.items(),
                key=lambda kv: (float(kv[1][0]) if isinstance(kv[1], tuple) else float(kv[1])),
            )[:overflow]:
                self.ocr_seen_fish.pop(key, None)

        stale_fish_exp = [key for key in self.ocr_seen_fish_exp.keys() if key not in self.ocr_seen_fish]
        for key in stale_fish_exp:
            self.ocr_seen_fish_exp.pop(key, None)

        if len(self.ocr_seen_fish_exp) > self.cfg.ocr_seen_max_entries:
            overflow = len(self.ocr_seen_fish_exp) - self.cfg.ocr_seen_max_entries
            for key, _ in sorted(self.ocr_seen_fish.items(), key=lambda kv: kv[1])[:overflow]:
                if key in self.ocr_seen_fish_exp:
                    self.ocr_seen_fish_exp.pop(key, None)

    def _process_notifications_ocr(self, frame_bgr: np.ndarray) -> None:
        if not self.cfg.ocr_enabled:
            return

        if not self.ocr_available:
            if self.ocr_init_error:
                self._log_ocr_warning("OCR disabled: %s", self.ocr_init_error)
            return

        self._drain_ocr_results(frame_bgr)

        now_ts = time.time()
        if (now_ts - self.ocr_last_ts) < max(0.1, self.cfg.ocr_interval_sec):
            return
        self.ocr_last_ts = now_ts

        roi, (x0, y0, x1, y1) = self._get_notification_roi(frame_bgr)
        if roi.size == 0:
            return

        if self.cfg.ocr_use_worker:
            payload = (now_ts, roi.copy(), (x0, y0, x1, y1))
            try:
                self.ocr_request_queue.put_nowait(payload)
            except queue.Full:
                try:
                    self.ocr_request_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.ocr_request_queue.put_nowait(payload)
                except queue.Full:
                    pass
            return

        try:
            raw_text = self._ocr_read_roi(roi)
        except Exception as exc:
            msg = str(exc)
            msg_l = msg.lower()
            if "tesseract is not installed" in msg_l or "not in your path" in msg_l:
                self._configure_ocr_backend()
                if not self.ocr_available:
                    self._log_ocr_warning("OCR disabled: %s", self.ocr_init_error or msg)
                return
            self._log_ocr_warning("OCR read failed: %s", exc)
            return

        self._handle_ocr_text_result(
            raw_text=raw_text,
            error_text=None,
            roi_box=(x0, y0, x1, y1),
            frame_bgr=frame_bgr,
        )

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

    @staticmethod
    def _ru_items_word(value: int) -> str:
        n = abs(int(value))
        if (n % 10) == 1 and (n % 100) != 11:
            return "штука"
        if 2 <= (n % 10) <= 4 and not (12 <= (n % 100) <= 14):
            return "штуки"
        return "штук"

    def _build_ocr_fish_summary(self) -> str:
        if not self.ocr_fish_counter:
            return "-"

        lines = []
        sorted_items = sorted(self.ocr_fish_counter.items(), key=lambda kv: (-int(kv[1]), kv[0]))
        for fish_name, count in sorted_items:
            lines.append(f"{fish_name} - {int(count)} {self._ru_items_word(int(count))}")
        return "\n".join(lines)

    def _finalize_cycle(self, fish_caught: bool, reason: str) -> None:
        self.cycles_total += 1
        if fish_caught:
            self.fish_caught_total += 1

        ocr_fish_total = int(sum(self.ocr_fish_counter.values()))
        fish_summary = self._build_ocr_fish_summary()

        print(
            f"\nEXP - {self.ocr_total_exp}\n"
            f"Рыбы:\n{fish_summary}\n"
            f"\nCycle #{self.cycles_total} ({reason}) | Total fish: {self.fish_caught_total} "
            f"| OCR fish: {ocr_fish_total} | OCR XP: {self.ocr_total_exp}"
        )
        self.logger.info(
            "Cycle #%d finished (%s). Total fish=%d OCR fish=%d OCR XP=%d",
            self.cycles_total,
            reason,
            self.fish_caught_total,
            ocr_fish_total,
            self.ocr_total_exp,
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
                    self._process_notifications_ocr(frame)

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
