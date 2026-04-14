import time
import numpy as np
import mss
import pyautogui
import keyboard
import cv2
from collections import deque

# Настройки
pyautogui.PAUSE = 0
pyautogui.MINIMUM_DURATION = 0

# Разрешение экрана
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
CENTER_X = SCREEN_WIDTH // 2
CENTER_Y = SCREEN_HEIGHT // 2

# Область поиска (центр экрана ±400 пикселей)
SEARCH_RADIUS = 400

# Режим дебага
DEBUG_MODE = False

# Кэш для HSV конвертации
_hsv_cache = None
_hsv_cache_id = None

# HSV диапазоны (конвертируем BGR в HSV для проверки)
# Точные цвета зеленого в мини-играх (ПО РЕАЛЬНЫМ ДАННЫМ ИЗ ИГРЫ)
# !!! ВАЖНО: В OpenCV порядок BGR, а не RGB !!!
# 
# Мини-игра 1: РЕАЛЬНЫЕ значения из логов:
#   B: 11-24, G: 64-102, R: 2-30
#   Это ТЕМНО-ЗЕЛЕНЫЙ (не яркий!)
# 
# Мини-игра 2: RGB(2, 184, 8) -> BGR(8, 184, 2)

def get_hue_fast(b, g, r):
    """
    Быстрая конвертация BGR в HSV Hue (0-180 в OpenCV)
    Зеленый ≈ 60-90°, Красный ≈ 0-10° или 170-180°
    Это НАМНОГО стабильнее чем BGR пороги при разных освещениях!
    """
    max_c = np.maximum(np.maximum(b, g), r)
    min_c = np.minimum(np.minimum(b, g), r)
    delta = max_c - min_c

    hue = np.zeros_like(max_c, dtype=np.float32)

    mask_g = (g == max_c) & (delta > 0)
    hue[mask_g] = 60.0 * (((b[mask_g] - r[mask_g]) / delta[mask_g]) % 6)

    mask_r = (r == max_c) & (delta > 0)
    hue[mask_r] = 60.0 * (((g[mask_r] - b[mask_r]) / delta[mask_r]) + 0)

    mask_b = (b == max_c) & (delta > 0)
    hue[mask_b] = 60.0 * (((r[mask_b] - g[mask_b]) / delta[mask_b]) + 4)

    hue = np.clip(hue, 0, 180).astype(np.uint8)
    return hue

def create_green_mask_by_hue(roi_bgr):
    """
    Маска зеленого ТОЛЬКО по Hue в HSV (независимо от яркости)
    Зеленый в игре обычно имеет Hue 50-95 (в диапазоне 0-180 OpenCV)
    """
    b = roi_bgr[:, :, 0].astype(np.float32)
    g = roi_bgr[:, :, 1].astype(np.float32)
    r = roi_bgr[:, :, 2].astype(np.float32)

    hue = get_hue_fast(b, g, r)
    
    max_c = np.maximum(np.maximum(b, g), r)
    min_c = np.minimum(np.minimum(b, g), r)
    saturation = np.where(max_c > 0, (max_c - min_c) / max_c, 0)

    mask = (hue >= 45) & (hue <= 95) & (saturation >= 0.35)
    return mask

def create_red_mask_by_hue(roi_bgr):
    """
    Маска красного ТОЛЬКО по Hue в HSV
    Красный имеет Hue 0-10 или 170-180
    """
    b = roi_bgr[:, :, 0].astype(np.float32)
    g = roi_bgr[:, :, 1].astype(np.float32)
    r = roi_bgr[:, :, 2].astype(np.float32)

    hue = get_hue_fast(b, g, r)
    
    max_c = np.maximum(np.maximum(b, g), r)
    min_c = np.minimum(np.minimum(b, g), r)
    saturation = np.where(max_c > 0, (max_c - min_c) / max_c, 0)

    mask = ((hue <= 10) | (hue >= 170)) & (saturation >= 0.30)
    return mask

def is_green_pixel_game1(b, g, r):
    """
    Проверка зеленого для мини-игры 1
    Используем тот же диапазон что и для игры 2 (яркий зеленый)
    """
    # Темно-зеленый сегмент в первой мини-игре часто имеет G около 70-120.
    # Добавляем проверку доминирования G над R/B, чтобы не захватывать серый фон.
    return (0 <= b <= 120 and
            60 <= g <= 170 and
            0 <= r <= 110 and
            g >= b + 8 and
            g >= r + 8)

def is_green_pixel_game2(b, g, r):
    """
    Проверка зеленого для мини-игры 2 (яркий)
    Целевой: RGB(2, 184, 8) = BGR(8, 184, 2)
    """
    # Ярко-зеленый сегмент (вторая мини-игра)
    return (0 <= b <= 90 and
            125 <= g <= 255 and
            0 <= r <= 90 and
            g >= b + 20 and
            g >= r + 20)

def is_green_pixel(b, g, r):
    """Универсальная проверка зеленого (любая мини-игра)"""
    return is_green_pixel_game1(b, g, r) or is_green_pixel_game2(b, g, r)

def create_green_mask_fast(roi_bgr):
    """
    ОПТИМИЗИРОВАННАЯ маска зеленого: только BGR, без HSV сложности
    Зеленый должен явно доминировать над красным и синим
    """
    b = roi_bgr[:, :, 0].astype(np.int16)
    g = roi_bgr[:, :, 1].astype(np.int16)
    r = roi_bgr[:, :, 2].astype(np.int16)

    # Стратегия: две части спектра (темный и яркий зеленый)
    dark_green = (
        (b >= 5) & (b <= 130) &
        (g >= 65) & (g <= 180) &
        (r >= 0) & (r <= 115) &
        (g > b) & (g > r) &
        (g >= b + 12) & (g >= r + 12)
    )

    bright_green = (
        (b >= 0) & (b <= 95) &
        (g >= 120) & (g <= 255) &
        (r >= 0) & (r <= 95) &
        (g > b) & (g > r) &
        (g >= b + 18) & (g >= r + 18)
    )

    return dark_green | bright_green

def create_white_mask_fast(roi_bgr):
    """
    СУПЕР-БЫСТРАЯ маска белых пикселей через cv2.inRange
    """
    # Белый в игре часто "грязный" (слегка серый/желтый),
    # поэтому используем яркость + малый разброс каналов.
    b = roi_bgr[:, :, 0].astype(np.int16)
    g = roi_bgr[:, :, 1].astype(np.int16)
    r = roi_bgr[:, :, 2].astype(np.int16)

    max_c = np.maximum(np.maximum(b, g), r)
    min_c = np.minimum(np.minimum(b, g), r)
    spread = max_c - min_c

    # Исправлено: более правильные пороги для белого маркера-крючка
    # min_c > 120 (не требуем чтобы все каналы были > 130, допускаем 120)
    # max_c >= 130 (пик яркости минимум 130)
    # spread <= 50 (строгий контроль разброса, как в is_white_pixel)
    mask = (min_c > 120) & (max_c >= 130) & (spread <= 50)
    return mask

def create_red_mask_fast(roi_bgr):
    """
    ОПТИМИЗИРОВАННАЯ маска красного: только BGR, без HSV
    Красный должен явно доминировать над зеленым и синим
    """
    b = roi_bgr[:, :, 0].astype(np.int16)
    g = roi_bgr[:, :, 1].astype(np.int16)
    r = roi_bgr[:, :, 2].astype(np.int16)

    # Строго: R высокое, G и B низкие
    mask = (
        (r >= 90) &
        (g <= 125) &
        (b <= 125) &
        (r > g) & (r > b) &
        (r >= g + 35) &
        (r >= b + 35)
    )
    
    return mask

def is_red_pixel(b, g, r):
    """
    Проверка: красный ли пиксель
    Красный должен быть намного выше остальных
    """
    # R высокое, G/B заметно ниже и цвет не должен быть серым или грязно-коричневым.
    max_val = max(b, g, r)
    min_val = min(b, g, r)
    return r >= 95 and g <= 120 and b <= 120 and r >= g + 40 and r >= b + 40 and (max_val - min_val) >= 40

def is_white_pixel(b, g, r):
    """
    Проверка: белый ли пиксель (поплавок)
    """
    # Белый: все каналы высокие (>130) и близки друг к другу
    min_val = min(b, g, r)
    max_val = max(b, g, r)
    return min_val > 130 and (max_val - min_val) < 50

def is_gray_pixel(b, g, r):
    """Проверка: серый ли пиксель"""
    # Серый: средние значения, близкие друг к другу
    avg = (b + g + r) / 3
    return 40 < avg < 150 and abs(b - g) < 40 and abs(g - r) < 40


def smooth_position(pos, history):
    """Сглаживает позицию используя медианное значение из истории"""
    if pos is None:
        history.clear()
        return None, history
    
    history.append(pos)
    
    # Вычисляем медиану всех сохраненных позиций
    x_vals = np.array([p[0] for p in history])
    y_vals = np.array([p[1] for p in history])
    smoothed_x = int(np.median(x_vals))
    smoothed_y = int(np.median(y_vals))
    
    return (smoothed_x, smoothed_y), history


def park_cursor_in_corner(step_idx):
    """Уводит курсор в один из углов экрана, чтобы он не мешал распознаванию."""
    sw, sh = pyautogui.size()
    pad = 8
    corners = [
        (pad, pad),
        (sw - pad, pad),
        (sw - pad, sh - pad),
        (pad, sh - pad),
    ]
    idx = step_idx % len(corners)
    x, y = corners[idx]
    pyautogui.moveTo(x, y)
    return step_idx + 1


def find_horizontal_bar(screen_bgr):
    """
    Поиск горизонтальной полоски (мини-игра 1)
    Устойчивая схема: длинный зеленый сегмент + белый маркер внутри + красные края.
    """
    h, w = screen_bgr.shape[:2]
    search_w = 760
    search_h = 360
    # Полоска чаще появляется немного ниже геометрического центра кадра.
    y_offset = 50
    cx = w // 2
    cy = min(h - 1, (h // 2) + y_offset)

    x_start = max(0, cx - search_w // 2)
    x_end = min(w, cx + search_w // 2)
    y_start = max(0, cy - search_h // 2)
    y_end = min(h, cy + search_h // 2)

    roi = screen_bgr[y_start:y_end, x_start:x_end]
    if roi.size == 0:
        return None, 0

    roi_h, roi_w = roi.shape[:2]
    green_mask = create_green_mask_fast(roi)
    white_mask = create_white_mask_fast(roi)
    red_mask = create_red_mask_fast(roi)

    # Курсор мыши белый и может ложно сработать как маркер.
    # Вырезаем область вокруг текущего курсора из белой маски.
    mouse_x, mouse_y = pyautogui.position()
    mouse_rx = int(mouse_x - x_start)
    mouse_ry = int(mouse_y - y_start)
    if 0 <= mouse_rx < roi_w and 0 <= mouse_ry < roi_h:
        pad = 18
        mx0 = max(0, mouse_rx - pad)
        mx1 = min(roi_w, mouse_rx + pad + 1)
        my0 = max(0, mouse_ry - pad)
        my1 = min(roi_h, mouse_ry + pad + 1)
        white_mask[my0:my1, mx0:mx1] = False

    green_u8 = (green_mask.astype(np.uint8) * 255)
    kernel = np.ones((3, 3), np.uint8)
    green_u8 = cv2.morphologyEx(green_u8, cv2.MORPH_OPEN, kernel)
    green_u8 = cv2.morphologyEx(green_u8, cv2.MORPH_CLOSE, kernel)
    green_mask = green_u8 > 0

    best = None
    best_score = -10**9
    center_x = roi_w // 2
    center_y = roi_h // 2

    for y in range(4, roi_h - 4, 2):
        y0 = y - 2
        y1 = y + 3

        green_band = np.any(green_mask[y0:y1, :], axis=0)
        white_band = np.any(white_mask[y0:y1, :], axis=0)
        red_band = np.any(red_mask[y0:y1, :], axis=0)

        # Сначала находим целую полоску по красным краям.
        red_padded = np.pad(red_band.astype(np.int8), (1, 1), mode='constant')
        red_trans = np.diff(red_padded)
        red_starts = np.where(red_trans == 1)[0]
        red_ends = np.where(red_trans == -1)[0] - 1

        if len(red_starts) < 2:
            continue

        for i in range(len(red_starts) - 1):
            left_s = int(red_starts[i])
            left_e = int(red_ends[i])

            # Берем ближайший красный сегмент справа как второй край полоски.
            right_s = int(red_starts[i + 1])
            right_e = int(red_ends[i + 1])

            core_s = left_e + 1
            core_e = right_s - 1
            core_len = int(core_e - core_s + 1)

            if core_len < 65 or core_len > 430:
                continue

            # Зеленое тело полоски должно быть плотным и непрерывным.
            core_green_band = green_band[core_s:core_e + 1]
            if core_green_band.size == 0:
                continue
            core_green_line_ratio = float(np.mean(core_green_band))
            if core_green_line_ratio < 0.58:
                continue

            core_block = green_mask[y0:y1, core_s:core_e + 1]
            core_fill_ratio = float(np.mean(core_block)) if core_block.size > 0 else 0.0
            if core_fill_ratio < 0.36:
                continue

            left_red_ratio = float(np.mean(red_mask[y0:y1, left_s:left_e + 1])) if left_e >= left_s else 0.0
            right_red_ratio = float(np.mean(red_mask[y0:y1, right_s:right_e + 1])) if right_e >= right_s else 0.0
            if left_red_ratio < 0.22 or right_red_ratio < 0.22:
                continue

            # Теперь ищем белый маркер только внутри зеленой части.
            segment_white = white_band[core_s:core_e + 1]
            if not np.any(segment_white):
                continue

            wp = np.pad(segment_white.astype(np.int8), (1, 1), mode='constant')
            wt = np.diff(wp)
            w_starts = np.where(wt == 1)[0]
            w_ends = np.where(wt == -1)[0] - 1

            candidate_white_x = None
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
                white_col = white_mask[wy0:wy1, wx0:wx1]
                white_vertical = int(np.sum(np.any(white_col, axis=1))) if white_col.size > 0 else 0
                if white_vertical < 2 or white_vertical > 20:
                    continue

                dist_center = abs(wx - core_center)
                run_score = 120 - dist_center - run_len
                if run_score > candidate_score:
                    candidate_score = run_score
                    candidate_white_x = wx

            if candidate_white_x is None:
                continue

            white_x = candidate_white_x
            score = 0
            score += min(core_len, 240)
            score += int(core_fill_ratio * 160)
            score += int(core_green_line_ratio * 120)
            score += int((left_red_ratio + right_red_ratio) * 180)
            score -= abs(((core_s + core_e) // 2) - center_x) * 2
            score -= abs(y - center_y) * 3

            if score > best_score:
                best_score = score
                best = (white_x, y, core_s, core_e)

    if best is not None:
        white_x, y, s, _ = best

        # Уточняем Y по локальному белому кластеру возле маркера.
        rx0 = max(0, white_x - 8)
        rx1 = min(roi_w, white_x + 9)
        ry0 = max(0, y - 10)
        ry1 = min(roi_h, y + 11)
        white_local = white_mask[ry0:ry1, rx0:rx1]
        ys, _ = np.where(white_local)
        if len(ys) > 0:
            y = int(ry0 + np.median(ys))

        abs_x = white_x + x_start
        abs_y = y + y_start
        bar_width = max(0, white_x - s)
        return (abs_x, abs_y), bar_width

    return None, 0


def check_hook_in_green(screen_bgr, hook_pos):
    """
    Проверка: находится ли крючок в зеленой зоне (мини-игра 1)
    """
    if hook_pos is None:
        return False, 0.0
    
    x, y = hook_pos

    # Для мини-игры 1 надежнее смотреть горизонтальную полосу вокруг маркера,
    # а не только маленькое окно: белый маркер часто перекрывает зеленый под собой.
    x_start = max(0, x - 140)
    x_end = min(screen_bgr.shape[1], x + 141)
    y_start = max(0, y - 4)
    y_end = min(screen_bgr.shape[0], y + 5)

    roi = screen_bgr[y_start:y_end, x_start:x_end]
    if roi.size == 0:
        return False, 0.0

    green_mask = create_green_mask_fast(roi)
    red_mask = create_red_mask_fast(roi)

    # Локальные метрики вокруг центра маркера.
    # Окно делаем уже, чтобы не захватывать соседние (красные/зеленые) зоны полосы.
    cx = x - x_start
    cy = y - y_start
    center_half_w = 11
    center_half_h = 4

    cx0 = max(0, cx - center_half_w)
    cx1 = min(roi.shape[1], cx + center_half_w + 1)
    cy0 = max(0, cy - center_half_h)
    cy1 = min(roi.shape[0], cy + center_half_h + 1)

    center_green = green_mask[cy0:cy1, cx0:cx1]
    center_red = red_mask[cy0:cy1, cx0:cx1]

    center_green_ratio = float(np.mean(center_green)) if center_green.size > 0 else 0.0
    center_red_ratio = float(np.mean(center_red)) if center_red.size > 0 else 0.0

    # Проекция по X в узкой горизонтальной полосе.
    green_band = np.any(green_mask, axis=0)
    red_band = np.any(red_mask, axis=0)

    # Восстанавливаем зеленые сегменты (непрерывные участки).
    padded = np.pad(green_band.astype(np.int8), (1, 1), mode='constant')
    transitions = np.diff(padded)
    starts = np.where(transitions == 1)[0]
    ends = np.where(transitions == -1)[0] - 1

    in_green_segment = False
    nearest_seg = None
    if len(starts) > 0:
        # Берем сегмент, ближайший к X маркера.
        seg_centers = (starts + ends) // 2
        nearest_idx = int(np.argmin(np.abs(seg_centers - cx)))
        s = int(starts[nearest_idx])
        e = int(ends[nearest_idx])
        nearest_seg = (s, e)
        # Маркер должен быть внутри сегмента с небольшим запасом от края,
        # иначе это часто переходная зона к красному.
        seg_len = max(1, e - s + 1)
        edge_margin = max(3, int(seg_len * 0.10))
        in_green_segment = (s + edge_margin) <= cx <= (e - edge_margin)

    # Красный под самим маркером. Если он доминирует, клик рано.
    red_center_block = red_mask[cy0:cy1, cx0:cx1]
    red_center_ratio = float(np.mean(red_center_block)) if red_center_block.size > 0 else 0.0

    # Доп. контроль: если красная область слишком близко к крючку, это опасная зона.
    red_indices = np.where(red_band)[0]
    if len(red_indices) > 0:
        nearest_red_dist = int(np.min(np.abs(red_indices - cx)))
    else:
        nearest_red_dist = 999

    tight_half_w = 7
    tx0 = max(0, cx - tight_half_w)
    tx1 = min(roi.shape[1], cx + tight_half_w + 1)
    tight_green = green_mask[cy0:cy1, tx0:tx1]
    tight_red = red_mask[cy0:cy1, tx0:tx1]
    tight_green_ratio = float(np.mean(tight_green)) if tight_green.size > 0 else 0.0
    tight_red_ratio = float(np.mean(tight_red)) if tight_red.size > 0 else 0.0

    # Ужесточаем решение, чтобы не было кликов в красную область.
    # Нужно одновременно: геометрия сегмента + доминирование зеленого + удаленность от красных краев.
    local_green_ok = (
        (center_green_ratio >= 0.12 and center_green_ratio >= center_red_ratio * 1.35) or
        (center_green_ratio >= 0.20 and center_red_ratio < 0.12)
    )

    segment_has_safe_margin = False
    if nearest_seg is not None:
        s, e = nearest_seg
        segment_has_safe_margin = (cx - s >= 4) and (e - cx >= 4)

    red_safety_ok = (
        red_center_ratio < 0.12 and
        tight_red_ratio < 0.10 and
        nearest_red_dist >= 6
    )
    green_strength_ok = tight_green_ratio >= 0.14

    in_green = in_green_segment and segment_has_safe_margin and local_green_ok and red_safety_ok and green_strength_ok

    green_ratio_for_status = center_green_ratio
    return in_green, green_ratio_for_status


def find_vertical_scale(screen_bgr):
    """
    Поиск вертикальной шкалы (мини-игра 2) - УЛЬТРА-БЫСТРО
    Приоритет: ЦЕНТР ЭКРАНА (сканируем от центра наружу)
    """
    h, w = screen_bgr.shape[:2]
    
    # Уменьшенная область для скорости
    search_h = 500
    search_w = 400
    
    center_x = w // 2
    center_y = h // 2

    y_start = max(0, center_y - search_h // 2)
    y_end = min(h, center_y + search_h // 2)
    x_start = max(0, center_x - search_w // 2)
    x_end = min(w, center_x + search_w // 2)
    
    roi = screen_bgr[y_start:y_end, x_start:x_end]
    roi_h, roi_w = roi.shape[:2]
    
    # БЫСТРЫЕ маски
    green_mask = create_green_mask_fast(roi)
    
    if not np.any(green_mask):
        return None
    
    red_mask = create_red_mask_fast(roi)
    
    # Находим столбцы с зелеными пикселями
    green_columns = np.where(np.any(green_mask, axis=0))[0]
    
    if len(green_columns) == 0:
        return None
    
    # ПРИОРИТЕТ ЦЕНТРУ: Сортируем столбцы по близости к центру
    center_roi_x = roi_w // 2
    green_columns_sorted = sorted(green_columns, key=lambda x: abs(x - center_roi_x))
    
    # Проверяем столбцы от центра наружу, каждый 3-й
    for x_roi in green_columns_sorted[::3]:
        # Находим зеленые пиксели в этом столбце
        green_y_coords = np.where(green_mask[:, x_roi])[0]
        red_y_coords = np.where(red_mask[:, x_roi])[0]
        
        if len(green_y_coords) < 3 or len(red_y_coords) == 0:
            continue
        
        # Берем верхний зеленый и нижний красный
        green_y = green_y_coords[0]
        
        # Красный должен быть ниже зеленого минимум на 60 пикселей
        red_below = red_y_coords[red_y_coords > green_y + 60]
        
        if len(red_below) == 0:
            continue
        
        red_y = red_below[0]
        scale_height = red_y - green_y
        
        # Проверяем что у нас достаточно зеленых пикселей в начале
        green_count = np.sum(green_mask[green_y:green_y+80, x_roi])
        
        if green_count >= 3 and 80 < scale_height < 450:
            # Доп. фильтр формы шкалы: у реальной шкалы есть заметная ширина,
            # а не одиночная вертикальная линия как леска/удочка.
            xw0 = max(0, x_roi - 8)
            xw1 = min(roi_w, x_roi + 9)
            yw0 = max(0, green_y)
            yw1 = min(roi_h, red_y + 1)

            if yw1 <= yw0:
                continue

            col_activity = np.sum(green_mask[yw0:yw1, xw0:xw1], axis=0)
            active_cols = np.where(col_activity >= 6)[0]
            if len(active_cols) == 0:
                continue

            scale_width = int(active_cols[-1] - active_cols[0] + 1)
            if scale_width < 4 or scale_width > 26:
                continue

            # У нижнего края должна быть выраженная красная зона.
            red_bottom_y0 = max(0, red_y - 8)
            red_bottom_y1 = min(roi_h, red_y + 9)
            red_bottom_ratio = float(np.mean(red_mask[red_bottom_y0:red_bottom_y1, xw0:xw1]))
            if red_bottom_ratio < 0.06:
                continue

            abs_x = x_roi + x_start
            abs_green_y = green_y + y_start
            abs_red_y = red_y + y_start
            
            return {
                'x': abs_x,
                'top_y': abs_green_y,
                'height': scale_height,
                'green_y': abs_green_y,
                'red_y': abs_red_y
            }
    
    return None


def find_float_on_scale(screen_bgr, scale_info):
    """
    Поиск белого поплавка на вертикальной шкале - МАКСИМАЛЬНО БЫСТРО
    """
    if scale_info is None:
        return None, "unknown"
    
    x = scale_info['x']
    top_y = scale_info['top_y']
    height = scale_info['height']
    
    # Узкий ROI вокруг шкалы (±15 пикселей)
    x_start = max(0, x - 15)
    x_end = min(screen_bgr.shape[1], x + 16)
    y_start = top_y
    y_end = min(screen_bgr.shape[0], top_y + height)
    
    roi = screen_bgr[y_start:y_end, x_start:x_end]
    
    # БЫСТРАЯ маска через cv2.inRange
    white_mask = create_white_mask_fast(roi)
    
    # Находим все белые пиксели
    white_rows = np.where(np.any(white_mask, axis=1))[0]
    
    if len(white_rows) > 0:
        # Берем первый белый пиксель (сверху)
        y_roi = white_rows[0]
        y_abs = y_roi + y_start
        
        # Определяем относительную позицию
        relative_pos = (y_abs - top_y) / height
        
        # Определяем зону с более агрессивными границами
        if relative_pos < 0.4:  # Увеличено с 0.35
            zone = "green"
        elif relative_pos < 0.65:
            zone = "gray"
        else:
            zone = "red"
        
        return relative_pos, zone
    
    return None, "unknown"


def visualize_detection(screen_bgr):
    """
    Визуализация: показывает что бот видит
    Использует БЫСТРЫЕ маски для проверки
    """
    h, w = screen_bgr.shape[:2]
    
    # Создаем копию для рисования
    visual = screen_bgr.copy()
    
    # Центральная область поиска
    x_center = w // 2
    y_center = h // 2
    x_start = max(0, x_center - 300)
    x_end = min(w, x_center + 300)
    y_start = max(0, y_center - 150)
    y_end = min(h, y_center + 150)
    
    # Извлекаем ROI
    roi = screen_bgr[y_start:y_end, x_start:x_end]
    
    # Рисуем рамку области поиска
    cv2.rectangle(visual, (x_start, y_start), (x_end, y_end), (255, 255, 0), 2)
    
    # БЫСТРЫЕ маски
    green_mask = create_green_mask_fast(roi)
    white_mask = create_white_mask_fast(roi)
    red_mask = create_red_mask_fast(roi)
    
    game_pixels = np.sum(green_mask)
    white_pixels = np.sum(white_mask)
    red_pixels = np.sum(red_mask)
    
    # Накладываем маски на визуализацию
    roi_visual = visual[y_start:y_end, x_start:x_end]
    roi_visual[green_mask] = [0, 255, 0]  # Зеленые пиксели
    roi_visual[white_mask] = [255, 255, 255]  # Белые пиксели
    roi_visual[red_mask] = [0, 0, 255]  # Красные пиксели
    
    # Добавляем текст со статистикой
    cv2.putText(visual, f"Green: {game_pixels} px", 
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    cv2.putText(visual, f"White: {white_pixels} px", 
                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(visual, f"Red: {red_pixels} px", 
                (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    
    # Пытаемся найти полоску
    bar_pos, bar_width = find_horizontal_bar(screen_bgr)
    if bar_pos:
        x, y = bar_pos
        cv2.circle(visual, (x, y), 10, (255, 0, 255), 3)  # Фиолетовый круг
        cv2.putText(visual, f"BAR FOUND! ({x}, {y}) W:{bar_width}px", 
                    (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2)
    else:
        cv2.putText(visual, "BAR NOT FOUND", 
                    (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    
    # Сохраняем во временный файл
    cv2.imwrite("debug_visualization.png", visual)
    print("\n" + "=" * 60)
    print("📸 ВИЗУАЛИЗАЦИЯ СОХРАНЕНА: debug_visualization.png")
    print(f"   Зеленый: {game_pixels} пикселей")
    print(f"   Белый:   {white_pixels} пикселей")
    print(f"   Красный: {red_pixels} пикселей")
    if bar_pos:
        print(f"   ✅ Полоска НАЙДЕНА на ({bar_pos[0]}, {bar_pos[1]})")
    else:
        print(f"   ❌ Полоска НЕ НАЙДЕНА")
    print("=" * 60 + "\n")



    """Захват экрана в BGR"""
    monitor = sct.monitors[1]
    frame = np.array(sct.grab(monitor))
    # BGRA -> BGR
    return frame[:, :, [2, 1, 0]]


def get_screen(sct):
    """Захват экрана в BGR формате (ОПТИМИЗИРОВАНО)"""
    monitor = sct.monitors[1]
    frame = np.array(sct.grab(monitor))
    # Прямая нарезка массива быстрее чем cv2.cvtColor
    return frame[:, :, :3]  # Берем только BGR, отбрасываем альфа


def get_screen_with_alpha(sct):
    """Захват экрана с альфа-каналом (BGRA)"""
    monitor = sct.monitors[1]
    frame = np.array(sct.grab(monitor))
    return frame  # BGRA формат


def get_pixel_color_at_mouse():
    """Получить цвет пикселя под курсором мыши"""
    with mss.mss() as sct:
        mouse_x, mouse_y = pyautogui.position()
        
        # Захватываем с альфа-каналом
        frame = get_screen_with_alpha(sct)
        
        if 0 <= mouse_y < frame.shape[0] and 0 <= mouse_x < frame.shape[1]:
            # BGRA формат
            b, g, r, a = frame[mouse_y, mouse_x]
            return (b, g, r, a), (mouse_x, mouse_y)
    
    return None, None


def print_color_info(bgra, pos):
    """Красивый вывод информации о цвете"""
    if bgra is None:
        return
    
    b, g, r, a = bgra
    x, y = pos
    
    print("\n" + "=" * 60)
    print(f"🎨 ЦВЕТ ПИКСЕЛЯ в позиции ({x}, {y})")
    print("=" * 60)
    print(f"BGR:  B={b:3d}, G={g:3d}, R={r:3d}")
    print(f"RGB:  R={r:3d}, G={g:3d}, B={b:3d}")
    print(f"RGBA: R={r:3d}, G={g:3d}, B={b:3d}, A={a:3d}")
    print(f"Alpha: {a}/255 ({a/255*100:.1f}% непрозрачности)")
    print("-" * 60)
    
    # Проверяем соответствие нашим диапазонам
    is_g1 = is_green_pixel_game1(b, g, r)
    is_g2 = is_green_pixel_game2(b, g, r)
    
    if is_g1:
        print("✅ СОВПАДАЕТ с диапазоном мини-игры 1")
        print(f"   Диапазон: B:0-120, G:60-170, R:0-110 + доминирование G")
    elif is_g2:
        print("✅ СОВПАДАЕТ с диапазоном мини-игры 2")
        print(f"   Диапазон: B:0-90, G:125-255, R:0-90 + доминирование G")
    else:
        print("❌ НЕ совпадает ни с одним диапазоном")
        
        # Показываем насколько далеко от диапазонов
        print("\nПроверка диапазона игры 1 (ТЕМНЫЙ):")
        print(f"  B: {b:3d} {'✓' if 0 <= b <= 120 else '✗'} (нужно 0-120)")
        print(f"  G: {g:3d} {'✓' if 60 <= g <= 170 else '✗'} (нужно 60-170)")
        print(f"  R: {r:3d} {'✓' if 0 <= r <= 110 else '✗'} (нужно 0-110)")
        print(f"  G доминирует: {'✓' if (g >= b + 8 and g >= r + 8) else '✗'} (нужно G >= B+8 и G >= R+8)")
        
        print("\nПроверка диапазона игры 2 (ЯРКИЙ):")
        print(f"  B: {b:3d} {'✓' if 0 <= b <= 90 else '✗'} (нужно 0-90)")
        print(f"  G: {g:3d} {'✓' if 125 <= g <= 255 else '✗'} (нужно 125-255)")
        print(f"  R: {r:3d} {'✓' if 0 <= r <= 90 else '✗'} (нужно 0-90)")
        print(f"  G доминирует: {'✓' if (g >= b + 20 and g >= r + 20) else '✗'} (нужно G >= B+20 и G >= R+20)")
    
    print("=" * 60 + "\n")


print("=" * 60)
print("    FISHING BOT v23.1 - ULTRA-FAST & CENTER-FOCUSED")
print("=" * 60)
print("Улучшения:")
print("  ⚡ cv2.inRange для масок (3x быстрее)")
print("  🎯 ПРИОРИТЕТ ЦЕНТРА (от центра наружу)")
print("  🔍 Уменьшенные области поиска")
print("  🚀 Полная векторизация всех функций")
print("-" * 60)
print("Цветовые диапазоны (РАСШИРЕННЫЕ):")
print("  Зеленый: темный B:0-120 G:60-170 R:0-110 + G доминирует")
print("           яркий B:0-90 G:125-255 R:0-90 + G доминирует")
print("  Белый:   B:130-255, G:130-255, R:130-255")
print("  Красный: R>80 и R>1.3*G и R>1.3*B")
print("-" * 60)
print("УПРАВЛЕНИЕ:")
print("  [7] START/PAUSE - Старт/Пауза бота")
print("  [8] EXIT        - Выход")
print("  [9] COLOR       - Показать цвет под курсором (BGR+RGBA)")
print("  [0] DEBUG       - Вкл/Выкл режим дебага")
print("  [5] VISUAL      - Визуализация (что видит бот)")
print("=" * 60)

active = False
debug_mode = False

# Сглаживание позиции маркера (дека с последними N координатами)
hook_position_history = deque(maxlen=3)

# Защита от ложных кликов: нужен стабильный "зеленый" несколько кадров подряд.
green_confirm_count = 0
last_confirmed_hook_pos = None
cursor_corner_step = 0

with mss.mss() as sct:
    while True:
        if keyboard.is_pressed("8"):
            print("\n[EXIT] Остановка...")
            break
        
        # [9] - Показать цвет под курсором
        if keyboard.is_pressed("9"):
            bgra, pos = get_pixel_color_at_mouse()
            print_color_info(bgra, pos)
            time.sleep(0.5)  # Задержка чтобы не спамить
            continue
        
        # [0] - Переключить режим дебага
        if keyboard.is_pressed("0"):
            debug_mode = not debug_mode
            print(f"\n{'=' * 60}")
            print(f"🐛 DEBUG MODE: {'🟢 ВКЛ' if debug_mode else '🔴 ВЫКЛ'}")
            print(f"{'=' * 60}")
            time.sleep(0.5)
            continue
        
        # [5] - Визуализация (что видит бот)
        if keyboard.is_pressed("5"):
            screen_bgr = get_screen(sct)
            visualize_detection(screen_bgr)
            time.sleep(0.5)
            continue
        
        if keyboard.is_pressed("7"):
            active = not active
            print(f"\n{'=' * 60}")
            print(f"Статус: {'🟢 АКТИВЕН' if active else '🔴 ПАУЗА'}")
            print(f"{'=' * 60}")
            time.sleep(0.5)
        
        if not active:
            time.sleep(0.1)
            continue
        
        # ========== ФАЗА 1: Поиск горизонтальной полоски ==========
        screen = get_screen(sct)
        hook_pos, bar_width = find_horizontal_bar(screen)
        
        # Сглаживаем позицию маркера медианным фильтром
        if hook_pos:
            hook_pos, hook_position_history = smooth_position(hook_pos, hook_position_history)
        
        if hook_pos:
            # Проверяем находится ли крючок в зеленой зоне
            in_green, green_ratio = check_hook_in_green(screen, hook_pos)

            # Условие кандидата на клик: зеленый подтвержден и ширина разумная.
            click_candidate = in_green and (10 <= bar_width <= 220) and (green_ratio >= 0.12)
            if click_candidate:
                if last_confirmed_hook_pos is not None:
                    dx = abs(hook_pos[0] - last_confirmed_hook_pos[0])
                    dy = abs(hook_pos[1] - last_confirmed_hook_pos[1])
                    stable_pos = (dx <= 20 and dy <= 10)
                else:
                    stable_pos = True

                if stable_pos:
                    green_confirm_count += 1
                else:
                    green_confirm_count = 1
                last_confirmed_hook_pos = hook_pos
            else:
                green_confirm_count = 0
                last_confirmed_hook_pos = None

            high_confidence_green = in_green and green_ratio >= 0.38 and (14 <= bar_width <= 220)
            can_click_now = (green_confirm_count >= 2) or high_confidence_green
            
            status = f"🎣 Полоска найдена (ширина: {bar_width}px) | "
            status += f"Зел: {green_ratio:.2%} | {'🟢 КЛИК!' if can_click_now else '⚪ Ожидание'}"
            print(status + "     ", end="\r")
            
            # DEBUG: Показать детали
            if debug_mode:
                x, y = hook_pos
                b, g, r = screen[y, x]
                print(f"\n[DEBUG] Крючок at ({x},{y}) | BGR=({b},{g},{r}) | Зел={green_ratio:.2%}")
            
            if can_click_now:
                print(f"\n[!] КЛИК! Крючок в зеленой зоне at {hook_pos}")
                if debug_mode:
                    print(f"[DEBUG] Найдено {green_ratio:.2%} зеленых пикселей вокруг крючка")
                
                pyautogui.click(hook_pos)
                green_confirm_count = 0
                last_confirmed_hook_pos = None
                time.sleep(0.2)
                
                # ========== ФАЗА 2: Ожидание вертикальной шкалы ==========
                print("\n[...] Ожидание вертикальной шкалы (до 180 сек)...")
                time.sleep(2.0)  # Базовая задержка для появления UI
                
                wait_start = time.time()
                scale_found = False
                confirm_count = 0
                
                while time.time() - wait_start < 180 and active:
                    screen = get_screen(sct)
                    scale_info = find_vertical_scale(screen)
                    
                    elapsed = int(time.time() - wait_start)
                    
                    if scale_info:
                        # Подтверждаем шкалу только если видим белый поплавок в ее пределах.
                        float_pos, _ = find_float_on_scale(screen, scale_info)
                        float_confirmed = (float_pos is not None) and (0.0 <= float_pos <= 1.05)

                        if float_confirmed:
                            confirm_count += 1
                        else:
                            confirm_count = 0

                        h = scale_info['height']
                        status = f"📊 Шкала: {h}px | поплавок: {'да' if float_confirmed else 'нет'} | подтв: {confirm_count}/3"
                        print(f"{status} ({elapsed}s)     ", end="\r")
                        
                        if debug_mode:
                            print(f"\n[DEBUG] Шкала: X={scale_info['x']}, Top={scale_info['top_y']}, H={h}")
                        
                        if confirm_count >= 3:
                            print(f"\n[!] ШКАЛА ПОДТВЕРЖДЕНА! Начало мини-игры 2...")
                            scale_found = True
                            time.sleep(0.3)
                            break
                    else:
                        confirm_count = 0
                        print(f"🔍 Поиск шкалы... ({elapsed}s)     ", end="\r")
                    
                    if keyboard.is_pressed("8"):
                        break
                    
                    time.sleep(0.1)
                
                if not scale_found:
                    print(f"\n[-] Тайм-аут ожидания шкалы")
                    cursor_corner_step = park_cursor_in_corner(cursor_corner_step)
                    continue
                
                # ========== ФАЗА 3: Мини-игра со шкалой ==========
                print("\n[+++] МИНИ-ИГРА 2: Поднимаем поплавок!")
                minigame_start = time.time()
                clicks_count = 0
                lost_count = 0
                
                while time.time() - minigame_start < 60 and active:
                    screen = get_screen(sct)
                    scale_info = find_vertical_scale(screen)
                    
                    if not scale_info:
                        lost_count += 1
                        if lost_count >= 10:
                            print(f"\n[✓] Шкала исчезла. Мини-игра завершена! (кликов: {clicks_count})")
                            break
                        time.sleep(0.05)
                        continue
                    
                    lost_count = 0
                    
                    # Ищем поплавок
                    float_pos, zone = find_float_on_scale(screen, scale_info)
                    
                    if float_pos is not None:
                        in_green_zone = (zone == "green") or (float_pos < 0.35)
                        
                        zone_emoji = {"green": "🟢", "gray": "⚫", "red": "🔴", "unknown": "❓"}
                        
                        if debug_mode and clicks_count % 50 == 0:
                            print(f"\n[DEBUG] Поплавок: pos={float_pos:.3f}, zone={zone}, in_green={in_green_zone}")
                        
                        if not in_green_zone:
                            # Спамим клики для подъема
                            pyautogui.click()
                            clicks_count += 1
                            status = f"{zone_emoji.get(zone, '❓')} Поз: {float_pos:.2f} | {zone.upper()} | ⬆ ПОДЪЕМ | #{clicks_count}"
                            print(status + "    ", end="\r")
                            time.sleep(0.002)  # Еще быстрее
                        else:
                            # Медленные клики для удержания
                            pyautogui.click()
                            clicks_count += 1
                            status = f"🟢 Поз: {float_pos:.2f} | ЗЕЛЕНАЯ | ✓ УДЕРЖАНИЕ | #{clicks_count}"
                            print(status + "    ", end="\r")
                            time.sleep(0.035)  # Чуть быстрее
                    else:
                        # Не нашли поплавок - кликаем осторожно
                        pyautogui.click()
                        clicks_count += 1
                        print(f"❓ Поиск поплавка... #{clicks_count}     ", end="\r")
                        time.sleep(0.01)
                    
                    if keyboard.is_pressed("8"):
                        break
                
                print(f"\n[✓] Цикл завершен! Ожидание 4 сек...")
                cursor_corner_step = park_cursor_in_corner(cursor_corner_step)
                time.sleep(4.0)
                green_confirm_count = 0
                last_confirmed_hook_pos = None
        else:
            green_confirm_count = 0
            last_confirmed_hook_pos = None
            print("🔍 Поиск горизонтальной полоски...     ", end="\r")
        
        time.sleep(0.05)

print("\n[✓] Бот остановлен.")
