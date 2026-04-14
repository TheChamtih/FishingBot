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

# Морфологические ядра (создаем один раз для скорости)
MORPH_KERNEL_SMALL = np.ones((3, 3), np.uint8)
MORPH_KERNEL_MEDIUM = np.ones((5, 5), np.uint8)

# HSV диапазоны (конвертируем BGR в HSV для проверки)
# Точные цвета зеленого в мини-играх (ПО РЕАЛЬНЫМ ДАННЫМ ИЗ ИГРЫ)
# !!! ВАЖНО: В OpenCV порядок BGR, а не RGB !!!
# 
# Мини-игра 1: РЕАЛЬНЫЕ значения из логов:
#   B: 11-24, G: 64-102, R: 2-30
#   Это ТЕМНО-ЗЕЛЕНЫЙ (не яркий!)
# 
# Мини-игра 2: RGB(2, 184, 8) -> BGR(8, 184, 2)

def is_green_pixel_game1(b, g, r):
    """
    Проверка зеленого для мини-игры 1
    Используем тот же диапазон что и для игры 2 (яркий зеленый)
    """
    # Расширенный диапазон для лучшего распознавания
    return (0 <= b <= 60 and        # B: расширено до 60
            130 <= g <= 255 and     # G: расширено 130-255
            0 <= r <= 50)           # R: расширено до 50

def is_green_pixel_game2(b, g, r):
    """
    Проверка зеленого для мини-игры 2 (яркий)
    Целевой: RGB(2, 184, 8) = BGR(8, 184, 2)
    """
    # Расширенный диапазон
    return (0 <= b <= 60 and        
            130 <= g <= 255 and     
            0 <= r <= 50)

def is_green_pixel(b, g, r):
    """Универсальная проверка зеленого (любая мини-игра)"""
    return is_green_pixel_game1(b, g, r) or is_green_pixel_game2(b, g, r)

def create_green_mask_hsv(roi_bgr):
    """
    УЛУЧШЕННАЯ детекция через HSV - стабильнее при разном освещении!
    HSV лучше работает с оттенками чем BGR
    """
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    
    # Зеленый в HSV:
    # H (Hue/Оттенок): 40-85 - это зеленый диапазон
    # S (Saturation/Насыщенность): 50-255 - от блеклого до яркого
    # V (Value/Яркость): 50-255 - от темного до светлого
    lower_green = np.array([40, 50, 50], dtype=np.uint8)
    upper_green = np.array([85, 255, 255], dtype=np.uint8)
    
    mask = cv2.inRange(hsv, lower_green, upper_green)
    return mask

def clean_mask(mask):
    """
    Морфологическая фильтрация - убирает шум и объединяет области
    """
    # Убрать мелкие точки (шум)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, MORPH_KERNEL_SMALL)
    # Заполнить маленькие дырки
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, MORPH_KERNEL_SMALL)
    return mask

def create_green_mask_fast(roi_bgr):
    """
    СУПЕР-БЫСТРАЯ маска зеленых пикселей через cv2.inRange (BGR)
    Используем BGR вместо HSV - работает стабильнее для этой игры!
    """
    # Расширенный диапазон: B:0-60, G:130-255, R:0-50
    lower_green = np.array([0, 130, 0], dtype=np.uint8)
    upper_green = np.array([60, 255, 50], dtype=np.uint8)
    
    mask = cv2.inRange(roi_bgr, lower_green, upper_green)
    return mask > 0

def create_dual_green_masks(roi_bgr):
    """
    Раздельные маски для темного и яркого зеленого (более точная детекция)
    """
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    
    # Темно-зеленый (низкая яркость V: 50-150)
    dark_mask = cv2.inRange(hsv, 
                            np.array([40, 50, 50], dtype=np.uint8),
                            np.array([85, 255, 150], dtype=np.uint8))
    
    # Ярко-зеленый (высокая яркость V: 150-255)
    bright_mask = cv2.inRange(hsv,
                              np.array([40, 50, 150], dtype=np.uint8),
                              np.array([85, 255, 255], dtype=np.uint8))
    
    # Очистка обеих масок
    dark_mask = clean_mask(dark_mask)
    bright_mask = clean_mask(bright_mask)
    
    return dark_mask > 0, bright_mask > 0# Конвертируем в boolean

def check_green_region(roi_bgr, x, y, radius=15):
    """
    Региональная проверка - проверяет окрестность вокруг точки
    Надежнее чем проверка одного пикселя!
    """
    h, w = roi_bgr.shape[:2]
    x1 = max(0, x - radius)
    x2 = min(w, x + radius)
    y1 = max(0, y - radius)
    y2 = min(h, y + radius)
    
    if x2 <= x1 or y2 <= y1:
        return False, 0.0
    
    region = roi_bgr[y1:y2, x1:x2]
    green_mask = create_green_mask_hsv(region)
    
    total_pixels = region.shape[0] * region.shape[1]
    green_pixels = np.sum(green_mask > 0)
    green_ratio = green_pixels / total_pixels if total_pixels > 0 else 0
    
    # Минимум 20% зеленых пикселей в регионе
    return green_ratio >= 0.20, green_ratio

def find_largest_green_contour(roi_bgr):
    """
    Контурный анализ - находит самую большую зеленую область
    Полезно когда несколько зеленых зон
    """
    mask = create_green_mask_hsv(roi_bgr)
    mask = clean_mask(mask)
    
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if len(contours) == 0:
        return None, 0
    
    # Находим самый большой контур
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    
    # Получаем центр контура
    M = cv2.moments(largest)
    if M["m00"] > 0:
        cx = int(M["m10"] / M["m00"])
        cy = int(M["m01"] / M["m00"])
        return (cx, cy), area
    
    return None, 0

def create_white_mask_fast(roi_bgr):
    """
    СУПЕР-БЫСТРАЯ маска белых пикселей через cv2.inRange
    """
    lower_white = np.array([130, 130, 130], dtype=np.uint8)
    upper_white = np.array([255, 255, 255], dtype=np.uint8)
    
    mask = cv2.inRange(roi_bgr, lower_white, upper_white)
    return mask > 0

def create_red_mask_fast(roi_bgr):
    """
    СУПЕР-БЫСТРАЯ маска красных пикселей
    """
    # Красный: высокое R, низкие G и B
    b, g, r = roi_bgr[:, :, 0], roi_bgr[:, :, 1], roi_bgr[:, :, 2]
    mask = (r > 80) & (r > g * 1.3) & (r > b * 1.3)
    return mask

def is_red_pixel(b, g, r):
    """
    Проверка: красный ли пиксель
    Красный должен быть намного выше остальных
    """
    # R высокое (>80), G и B низкие
    return r > 80 and r > g * 1.4 and r > b * 1.4

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


def find_horizontal_bar(screen_bgr):
    """
    Поиск горизонтальной полоски (мини-игра 1) - УЛЬТРА-ОПТИМИЗАЦИЯ
    Приоритет: ЦЕНТР ЭКРАНА (сканируем от центра наружу)
    Возвращает позицию белого маркера, ширину зеленой зоны и относительную
    позицию маркера в зеленой зоне (0.0 = левый край, 0.5 = центр, 1.0 = правый край)
    """
    h, w = screen_bgr.shape[:2]
    
    # Уменьшенная область поиска для скорости
    search_h = 300  # Вместо 800
    search_w = 600  # Вместо 800
    
    y_start = max(0, CENTER_Y - search_h // 2)
    y_end = min(h, CENTER_Y + search_h // 2)
    x_start = max(0, CENTER_X - search_w // 2)
    x_end = min(w, CENTER_X + search_w // 2)
    
    roi = screen_bgr[y_start:y_end, x_start:x_end]
    roi_h, roi_w = roi.shape[:2]
    
    # БЫСТРЫЕ маски через cv2.inRange
    green_mask = create_green_mask_fast(roi)
    white_mask = create_white_mask_fast(roi)
    red_mask = create_red_mask_fast(roi)

    # Если ключевые цвета не найдены, сразу выходим
    if not np.any(green_mask) or not np.any(white_mask) or not np.any(red_mask):
        return None, 0, None

    # Общая маска UI-полоски
    bar_mask = green_mask | white_mask | red_mask

    # Кандидаты строк: достаточно цветных пикселей и есть красный+белый
    row_color_count = np.sum(bar_mask, axis=1)
    row_white_count = np.sum(white_mask, axis=1)
    row_red_count = np.sum(red_mask, axis=1)

    candidate_rows = np.where(
        (row_color_count >= 120) &
        (row_white_count >= 1) &
        (row_red_count >= 12)
    )[0]

    if len(candidate_rows) == 0:
        return None, 0, None

    # ПРИОРИТЕТ ЦЕНТРУ: Сортируем строки по близости к центру
    center_roi_y = roi_h // 2
    center_roi_x = roi_w // 2
    rows_sorted = sorted(candidate_rows, key=lambda y: abs(y - center_roi_y))

    # Сканируем строки от центра, каждую 2-ю для скорости
    for y_roi in rows_sorted[::2]:
        row_mask = bar_mask[y_roi]
        x_coords = np.where(row_mask)[0]

        if len(x_coords) < 100:
            continue

        # Разбиваем строку на непрерывные сегменты с допуском маленьких разрывов
        gaps = np.diff(x_coords)
        split_idx = np.where(gaps > 2)[0]

        starts = np.insert(x_coords[split_idx + 1], 0, x_coords[0])
        ends = np.append(x_coords[split_idx], x_coords[-1])

        # Приоритет сегментам, которые ближе к центру
        seg_order = np.argsort(np.abs(((starts + ends) // 2) - center_roi_x))

        for idx in seg_order:
            seg_start = int(starts[idx])
            seg_end = int(ends[idx])
            seg_width = seg_end - seg_start + 1

            # Размеры полоски на скрине обычно в этом диапазоне
            if seg_width < 120 or seg_width > 420:
                continue

            # Внутри сегмента обязателен белый маркер
            seg_white = np.where(white_mask[y_roi, seg_start:seg_end + 1])[0]
            if len(seg_white) == 0:
                continue

            # Края сегмента должны быть красными
            cap = max(6, int(seg_width * 0.12))
            left_red = np.sum(red_mask[y_roi, seg_start:seg_start + cap])
            right_red = np.sum(red_mask[y_roi, seg_end - cap + 1:seg_end + 1])
            if left_red < 2 or right_red < 2:
                continue

            # В средней части должна быть зеленая зона
            mid_start = seg_start + seg_width // 5
            mid_end = seg_end - seg_width // 5
            if mid_end <= mid_start:
                continue

            mid_green = np.sum(green_mask[y_roi, mid_start:mid_end + 1])
            if mid_green < 4:
                continue

            # Берем белый пиксель, ближайший к центру экрана
            white_abs = seg_start + seg_white
            nearest_idx = np.argmin(np.abs(white_abs - center_roi_x))
            hook_x_roi = int(white_abs[nearest_idx])

            hook_rel = (hook_x_roi - seg_start) / max(1, seg_width - 1)
            abs_x = hook_x_roi + x_start
            abs_y = y_roi + y_start
            return (abs_x, abs_y), seg_width, hook_rel
    
    return None, 0, None


def check_hook_in_green(screen_bgr, hook_pos):
    """
    Проверка: находится ли крючок в зеленой зоне (мини-игра 1)
    УЛУЧШЕНО: Использует HSV и региональную проверку
    """
    if hook_pos is None:
        return False, 0.0
    
    x, y = hook_pos
    h, w = screen_bgr.shape[:2]
    
    # Создаем расширенный регион вокруг крючка
    radius_x = 80
    radius_y = 35
    
    x1 = max(0, x - radius_x)
    x2 = min(w, x + radius_x)
    y1 = max(0, y - radius_y)
    y2 = min(h, y + radius_y)
    
    region = screen_bgr[y1:y2, x1:x2]
    
    # HSV детекция зеленого
    green_mask = create_green_mask_hsv(region)
    green_mask = clean_mask(green_mask)
    
    total_pixels = region.shape[0] * region.shape[1]
    green_pixels = np.sum(green_mask > 0)
    green_ratio = green_pixels / total_pixels if total_pixels > 0 else 0
    
    # Крючок в зеленой зоне если >= 15% зеленых пикселей
    # (снижено с 2% для более точной детекции)
    return green_ratio >= 0.15, green_ratio


def find_vertical_scale(screen_bgr):
    """
    Поиск вертикальной шкалы (мини-игра 2) - УЛЬТРА-БЫСТРО
    Приоритет: ЦЕНТР ЭКРАНА (сканируем от центра наружу)
    """
    h, w = screen_bgr.shape[:2]
    
    # Уменьшенная область для скорости
    search_h = 500
    search_w = 400
    
    y_start = max(0, CENTER_Y - search_h // 2)
    y_end = min(h, CENTER_Y + search_h // 2)
    x_start = max(0, CENTER_X - search_w // 2)
    x_end = min(w, CENTER_X + search_w // 2)
    
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
    bar_pos, bar_width, hook_rel = find_horizontal_bar(screen_bgr)
    if bar_pos:
        x, y = bar_pos
        cv2.circle(visual, (x, y), 10, (255, 0, 255), 3)  # Фиолетовый круг
        rel_text = f"{hook_rel:.2f}" if hook_rel is not None else "n/a"
        cv2.putText(visual, f"BAR FOUND! ({x}, {y}) W:{bar_width}px Rel:{rel_text}", 
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
        print(f"   Диапазон: B:6-29, G:59-107, R:0-35")
    elif is_g2:
        print("✅ СОВПАДАЕТ с диапазоном мини-игры 2")
        print(f"   Диапазон: B:0-48, G:144-224, R:0-42")
    else:
        print("❌ НЕ совпадает ни с одним диапазоном")
        
        # Показываем насколько далеко от диапазонов
        print("\nПроверка диапазона игры 1 (ТЕМНЫЙ):")
        print(f"  B: {b:3d} {'✓' if 6 <= b <= 29 else '✗'} (нужно 6-29)")
        print(f"  G: {g:3d} {'✓' if 59 <= g <= 107 else '✗'} (нужно 59-107)")
        print(f"  R: {r:3d} {'✓' if 0 <= r <= 35 else '✗'} (нужно 0-35)")
        
        print("\nПроверка диапазона игры 2 (ЯРКИЙ):")
        print(f"  B: {b:3d} {'✓' if 0 <= b <= 48 else '✗'} (нужно 0-48)")
        print(f"  G: {g:3d} {'✓' if 144 <= g <= 224 else '✗'} (нужно 144-224)")
        print(f"  R: {r:3d} {'✓' if 0 <= r <= 42 else '✗'} (нужно 0-42)")
    
    print("=" * 60 + "\n")


print("=" * 60)
print("    FISHING BOT v24.0 - HSV DETECTION & MORPHOLOGY")
print("=" * 60)
print("Революционные улучшения:")
print("  🎨 HSV цветовое пространство (стабильнее)")
print("  🧹 Морфологическая фильтрация (без шума)")
print("  📊 Региональная проверка (надежнее)")
print("  🎯 Контурный анализ (точнее)")
print("  ⚡ ПРИОРИТЕТ ЦЕНТРА экрана")
print("-" * 60)
print("HSV диапазоны для зеленого:")
print("  H (Оттенок):      40-85  (зеленый спектр)")
print("  S (Насыщенность): 50-255 (любая яркость)")
print("  V (Яркость):      50-255 (темный и светлый)")
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
        hook_pos, bar_width, hook_rel = find_horizontal_bar(screen)
        
        if hook_pos:
            # Быстрый режим: ловим в центральном окне зеленой зоны
            # Это снижает задержку и не требует тяжелых дополнительных проверок.
            in_catch_window = hook_rel is not None and (0.22 <= hook_rel <= 0.78)
            center_quality = 1.0 - abs((hook_rel if hook_rel is not None else 0.5) - 0.5) * 2.0

            rel_text = f"{hook_rel:.2f}" if hook_rel is not None else "n/a"
            status = f"🎣 Полоска найдена (ширина: {bar_width}px) | "
            status += f"Rel: {rel_text} | Центр: {center_quality:.2f} | {'🟢 КЛИК!' if in_catch_window else '⚪ Центровка'}"
            print(status + "     ", end="\r")
            
            # DEBUG: Показать детали
            if debug_mode:
                x, y = hook_pos
                b, g, r = screen[y, x]
                print(f"\n[DEBUG] Крючок at ({x},{y}) | BGR=({b},{g},{r}) | Rel={rel_text} | Center={center_quality:.2f}")
            
            if in_catch_window:
                print(f"\n[!] КЛИК! Крючок в зеленой зоне at {hook_pos}")
                
                pyautogui.click(hook_pos)
                time.sleep(0.08)
                
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
                        confirm_count += 1
                        h = scale_info['height']
                        print(f"📊 Шкала обнаружена! ({elapsed}s) высота: {h}px | подтв: {confirm_count}/3     ", end="\r")
                        
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
                time.sleep(4.0)
        else:
            print("🔍 Поиск горизонтальной полоски...     ", end="\r")
        
        time.sleep(0.015)

print("\n[✓] Бот остановлен.")
