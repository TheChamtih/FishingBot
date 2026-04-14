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
    # Диапазон ±40 (как в игре 2)
    return (0 <= b <= 48 and        # B: 8 ±40
            144 <= g <= 224 and     # G: 184 ±40
            0 <= r <= 42)           # R: 2 ±40

def is_green_pixel_game2(b, g, r):
    """
    Проверка зеленого для мини-игры 2 (яркий)
    Целевой: RGB(2, 184, 8) = BGR(8, 184, 2)
    """
    # Диапазон ±40
    return (0 <= b <= 48 and        # B: 8 ±40
            144 <= g <= 224 and     # G: 184 ±40
            0 <= r <= 42)           # R: 2 ±40

def is_green_pixel(b, g, r):
    """Универсальная проверка зеленого (любая мини-игра)"""
    return is_green_pixel_game1(b, g, r) or is_green_pixel_game2(b, g, r)

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
    Поиск горизонтальной полоски (мини-игра 1)
    Ищем паттерн: зеленая зона + белый крючок + красная зона
    """
    h, w = screen_bgr.shape[:2]
    
    # Сканируем центральную область
    y_start = max(0, CENTER_Y - SEARCH_RADIUS)
    y_end = min(h, CENTER_Y + SEARCH_RADIUS)
    x_start = max(0, CENTER_X - SEARCH_RADIUS)
    x_end = min(w, CENTER_X + SEARCH_RADIUS)
    
    # Ищем горизонтальные линии с паттерном зеленый-белый-красный
    for y in range(y_start, y_end, 5):  # Шаг 5 для скорости
        green_found = False
        white_found = False
        red_found = False
        
        green_x = None
        white_x = None
        bar_y = None
        
        for x in range(x_start, x_end, 3):
            b, g, r = screen_bgr[y, x]
            
            if is_green_pixel(b, g, r):
                green_found = True
                if green_x is None:
                    green_x = x
                    bar_y = y
            
            elif green_found and is_white_pixel(b, g, r):
                white_found = True
                white_x = x
            
            elif green_found and white_found and is_red_pixel(b, g, r):
                red_found = True
                break
        
        # Если нашли паттерн - возвращаем позицию белого крючка
        if green_found and white_found and red_found and white_x:
            # Проверяем что нашли достаточно широкую полоску
            bar_width = abs(white_x - green_x)
            if bar_width > 50:  # Полоска должна быть минимум 50 пикселей
                return (white_x, bar_y), bar_width
    
    return None, 0


def check_hook_in_green(screen_bgr, hook_pos):
    """
    Проверка: находится ли крючок в зеленой зоне (мини-игра 1)
    """
    if hook_pos is None:
        return False, 0.0
    
    x, y = hook_pos
    
    # Расширенная область поиска (±70 пикселей по X, ±30 по Y)
    green_count = 0
    total_checked = 0
    
    for dx in range(-70, 71, 2):
        for dy in range(-30, 31, 2):
            check_x = x + dx
            check_y = y + dy
            
            if 0 <= check_y < screen_bgr.shape[0] and 0 <= check_x < screen_bgr.shape[1]:
                b, g, r = screen_bgr[check_y, check_x]
                total_checked += 1
                
                # Используем функцию для темного зеленого
                if is_green_pixel_game1(b, g, r):
                    green_count += 1
    
    # Крючок в зеленой зоне если >= 2% темно-зеленых пикселей
    green_ratio = green_count / total_checked if total_checked > 0 else 0
    return green_ratio >= 0.02, green_ratio


def find_vertical_scale(screen_bgr):
    """
    Поиск вертикальной шкалы (мини-игра 2)
    Ищет яркий зеленый RGB(2, 184, 8) вверху и красный внизу
    """
    h, w = screen_bgr.shape[:2]
    
    # Сканируем центральную область
    y_start = max(0, CENTER_Y - SEARCH_RADIUS)
    y_end = min(h, CENTER_Y + SEARCH_RADIUS)
    x_start = max(0, CENTER_X - SEARCH_RADIUS)
    x_end = min(w, CENTER_X + SEARCH_RADIUS)
    
    # Ищем вертикальные линии с паттерном зеленый-красный (сверху вниз)
    for x in range(x_start, x_end, 4):  # Шаг 4 для скорости
        green_y = None
        red_y = None
        green_count = 0
        
        for y in range(y_start, y_end, 2):
            b, g, r = screen_bgr[y, x]
            
            # Ищем яркий зеленый (специфичный для мини-игры 2)
            if green_y is None and is_green_pixel_game2(b, g, r):
                green_y = y
                green_count = 1
            elif green_y and (y - green_y) < 80 and is_green_pixel_game2(b, g, r):
                green_count += 1
            
            # Ищем красный ниже зеленого
            elif green_y and y > green_y + 60 and is_red_pixel(b, g, r):
                red_y = y
                break
        
        # Если нашли паттерн (достаточно зеленых пикселей + красный ниже)
        if green_y and red_y and green_count >= 5:
            scale_height = red_y - green_y
            
            # Высота шкалы должна быть разумной (80-450 пикселей)
            if 80 < scale_height < 450:
                return {
                    'x': x,
                    'top_y': green_y,
                    'height': scale_height,
                    'green_y': green_y,
                    'red_y': red_y
                }
    
    return None


def find_float_on_scale(screen_bgr, scale_info):
    """
    Поиск белого поплавка на вертикальной шкале
    """
    if scale_info is None:
        return None, "unknown"
    
    x = scale_info['x']
    top_y = scale_info['top_y']
    height = scale_info['height']
    
    # Сканируем вертикально по центру шкалы (±20 пикселей по X)
    for dx in range(-20, 21, 2):
        check_x = x + dx
        
        if check_x < 0 or check_x >= screen_bgr.shape[1]:
            continue
        
        for y in range(top_y, top_y + height, 2):
            if y >= screen_bgr.shape[0]:
                break
            
            b, g, r = screen_bgr[y, check_x]
            
            # Ищем белый пиксель (поплавок)
            if is_white_pixel(b, g, r):
                # Нашли белый пиксель - определяем позицию
                relative_pos = (y - top_y) / height
                
                # Определяем зону
                if relative_pos < 0.35:
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
    Зеленым выделяет пиксели игры 1, синим - игры 2
    """
    h, w = screen_bgr.shape[:2]
    
    # Создаем копию для рисования
    visual = screen_bgr.copy()
    
    # Центральная область поиска
    x_center = w // 2
    y_center = h // 2
    x_start = max(0, x_center - 400)
    x_end = min(w, x_center + 400)
    y_start = max(0, y_center - 200)
    y_end = min(h, y_center + 200)
    
    # Рисуем рамку области поиска
    cv2.rectangle(visual, (x_start, y_start), (x_end, y_end), (255, 255, 0), 2)
    
    game1_pixels = 0
    game2_pixels = 0
    
    # Сканируем и подсвечиваем совпадения
    for y in range(y_start, y_end, 2):
        for x in range(x_start, x_end, 2):
            b, g, r = screen_bgr[y, x]
            
            if is_green_pixel_game1(b, g, r):
                cv2.circle(visual, (x, y), 1, (0, 255, 0), -1)  # Зеленый для игры 1
                game1_pixels += 1
            elif is_green_pixel_game2(b, g, r):
                cv2.circle(visual, (x, y), 1, (255, 0, 0), -1)  # Синий для игры 2
                game2_pixels += 1
    
    # Добавляем текст со статистикой
    cv2.putText(visual, f"Game1 (dark green): {game1_pixels} px", 
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    cv2.putText(visual, f"Game2 (bright green): {game2_pixels} px", 
                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
    
    # Пытаемся найти полоску
    bar_pos, bar_width = find_horizontal_bar(screen_bgr)
    if bar_pos:
        x, y = bar_pos
        cv2.circle(visual, (x, y), 10, (0, 0, 255), 3)  # Красный круг на центре
        cv2.putText(visual, f"BAR FOUND! Width: {bar_width}px", 
                    (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    else:
        cv2.putText(visual, "BAR NOT FOUND", 
                    (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    
    # Сохраняем во временный файл
    cv2.imwrite("debug_visualization.png", visual)
    print("\n" + "=" * 60)
    print("📸 ВИЗУАЛИЗАЦИЯ СОХРАНЕНА: debug_visualization.png")
    print(f"   Игра 1 (темно-зеленый): {game1_pixels} пикселей")
    print(f"   Игра 2 (ярко-зеленый):  {game2_pixels} пикселей")
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
    """Захват экрана в BGR формате"""
    monitor = sct.monitors[1]
    frame = np.array(sct.grab(monitor))
    # Конвертируем BGRA -> BGR
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


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
print("    FISHING BOT v21.0 - SIMPLIFIED DETECTION")
print("=" * 60)
print("Цветовые диапазоны (ПО РЕАЛЬНЫМ ДАННЫМ):")
print("  Игра 1: B:6-29,    G:59-107,  R:0-35    (ТЕМНЫЙ)")
print("  Игра 2: B:0-48,    G:144-224, R:0-42    (ЯРКИЙ)")
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
        hook_pos, bar_width = find_horizontal_bar(screen)
        
        if hook_pos:
            # Проверяем находится ли крючок в зеленой зоне
            in_green, green_ratio = check_hook_in_green(screen, hook_pos)
            
            status = f"🎣 Полоска найдена (ширина: {bar_width}px) | "
            status += f"Зел: {green_ratio:.2%} | {'🟢 КЛИК!' if in_green else '⚪ Ожидание'}"
            print(status + "     ", end="\r")
            
            # DEBUG: Показать детали
            if debug_mode:
                x, y = hook_pos
                b, g, r = screen[y, x]
                print(f"\n[DEBUG] Крючок at ({x},{y}) | BGR=({b},{g},{r}) | Зел={green_ratio:.2%}")
            
            if in_green:
                print(f"\n[!] КЛИК! Крючок в зеленой зоне at {hook_pos}")
                if debug_mode:
                    print(f"[DEBUG] Найдено {green_ratio:.2%} зеленых пикселей вокруг крючка")
                
                pyautogui.click(hook_pos)
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
                            time.sleep(0.003)
                        else:
                            # Медленные клики для удержания
                            pyautogui.click()
                            clicks_count += 1
                            status = f"🟢 Поз: {float_pos:.2f} | ЗЕЛЕНАЯ | ✓ УДЕРЖАНИЕ | #{clicks_count}"
                            print(status + "    ", end="\r")
                            time.sleep(0.04)
                    else:
                        # Не нашли поплавок - кликаем осторожно
                        pyautogui.click()
                        clicks_count += 1
                        print(f"❓ Поиск поплавка... #{clicks_count}     ", end="\r")
                        time.sleep(0.01)
                    
                    if keyboard.is_pressed("8"):
                        break
                
                print(f"\n[✓] Цикл завершен! Ожидание 2 сек...")
                time.sleep(2.0)
        else:
            print("🔍 Поиск горизонтальной полоски...     ", end="\r")
        
        time.sleep(0.05)

print("\n[✓] Бот остановлен.")
