import os
import re
import time
import json
import base64
import hashlib
import requests
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


# ==========================================================
# 1. 기본 설정
# ==========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OJEONG_IMAGE_PATH = os.path.join(BASE_DIR, "오정메뉴.jpg")
OUTPUT_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "menu.json")
ROUTES_CACHE_JSON = os.path.join(OUTPUT_DIR, "routes_cache.json")
GEMINI_ATTEMPTS_JSON = os.path.join(OUTPUT_DIR, "gemini_attempts.json")

OFFICE_ADDRESS = "서울 금천구 가산디지털2로 30"
KAKAO_REST_KEY = os.environ.get("KAKAO_REST_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.6-flash"

# Gemini가 실패(특히 할당량 초과)했을 때의 보험용 폴백
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# openrouter/free는 이미지 인식이 되는 무료 모델 중 하나를 자동으로 골라주는 라우터
OPENROUTER_MODEL = "openrouter/free"

# 이 번호를 올리면, 오늘 이미 "성공"으로 저장된 캐시라도 무효화되고 새 코드로 다시 시도한다.
# (OCR 프롬프트/해상도/필터 로직을 고칠 때마다 하나씩 올려주면 됨)
EXTRACTION_PIPELINE_VERSION = 2

weekdays = ["월", "화", "수", "목", "금", "토", "일"]

today = datetime.now(ZoneInfo("Asia/Seoul"))
today_weekday_index = today.weekday()
today_weekday = weekdays[today_weekday_index]

today_date_str_space = f"{today.month}월 {today.day}일"
today_date_str_nospace = f"{today.month}월{today.day}일"

# 오정은 월~금 5개 컬럼
ojeong_weekday_index = min(today_weekday_index, 4)

print()
print("=" * 60)
print(f"오늘 날짜 : {today_date_str_space} ({today_weekday}요일)")
print("=" * 60)


# ==========================================================
# 2. 식당 정보 (전달해주신 정확한 위경도 좌표 적용)
# ==========================================================

cafeteria_list = [
    {
        "name": "오정",
        "address": "서울 금천구 가산디지털2로 30",
        "type": "ojeong",
        "url": OJEONG_IMAGE_PATH,
        "exact_lat": 37.47120020547345,
        "exact_lng": 126.88352033871539,
    },
    {
        "name": "온정찬",
        "address": "서울 금천구 가산디지털1로 75-15",
        "type": "kakao_posts",
        "url": "https://pf.kakao.com/_UIdXn/posts",
        "exact_lat": 37.47218918507373,
        "exact_lng": 126.88410160863671,
    },
    {
        "name": "런치투게더",
        "address": "서울 금천구 가산디지털1로 58",
        "type": "kakao_profile",
        "url": "https://pf.kakao.com/_swtYxl",
        "exact_lat": 37.471346435440815,
        "exact_lng": 126.88633772331427,
    },
    {
        "name": "런치타임",
        "address": "서울 금천구 가산디지털2로 24",
        "type": "instagram_threads",
        "instagram_url": "https://www.instagram.com/lunchtime_ypp/",
        "threads_url": "https://www.threads.net/@lunchtime_ypp",
        "exact_lat": 37.47089827252637,
        "exact_lng": 126.88388201555182,
    },
    {
        "name": "밥심",
        "address": "서울 금천구 가산디지털2로 46",
        "type": "kakao_first",
        "url": "https://pf.kakao.com/_mHWxjX",
        "exact_lat": 37.472650897653246,
        "exact_lng": 126.8826763789836,
    },
]


# ==========================================================
# 3. 오정 메뉴 - 요일별 이미지 Crop
# ==========================================================

def crop_ojeong_by_weekday(image_path):
    try:
        img = Image.open(image_path)
        width, height = img.size

        left_margin = width * 0.05
        right_margin = width * 0.95
        top_margin = height * 0.08
        bottom_margin = height * 0.92

        table_width = right_margin - left_margin
        col_width = table_width / 5

        crop_left = left_margin + (col_width * ojeong_weekday_index)
        crop_right = crop_left + col_width

        cropped_img = img.crop(
            (crop_left, top_margin, crop_right, bottom_margin)
        )

        def to_jpeg_bytes(im, max_height, quality):
            im2 = im
            if im2.height > max_height:
                ratio = max_height / im2.height
                im2 = im2.resize(
                    (int(im2.width * ratio), max_height),
                    Image.LANCZOS
                )
            buf = BytesIO()
            im2.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()

        # OCR용은 화질을 최대한 유지 (작게 줄이면 글자가 뭉개져서 오독이 심해짐)
        ocr_bytes = to_jpeg_bytes(cropped_img, max_height=1400, quality=95)

        # 화면 표시(폴백 이미지)용은 가볍게
        display_bytes = to_jpeg_bytes(cropped_img, max_height=420, quality=90)
        encoded_string = base64.b64encode(display_bytes).decode("utf-8")

        print(
            f"  -> [오정] {weekdays[ojeong_weekday_index]}요일 메뉴 Crop 완료"
        )

        return "data:image/jpeg;base64," + encoded_string, ocr_bytes

    except Exception as e:
        print(f"  -> [오정] Crop 실패 : {e}")
        return None, None


# ==========================================================
# 4. Selenium
# ==========================================================

def create_driver():
    options = Options()

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,1000")

    options.add_argument(
        "--user-agent=Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )

    return webdriver.Chrome(options=options)


# ==========================================================
# 5. 카카오 - 온정찬
# ==========================================================

def get_kakao_posts_image(driver, url):
    print("  -> [온정찬] 카카오 게시물 이미지 수집 중")

    try:
        driver.get(url)
        time.sleep(4)

        posts = driver.find_elements(By.TAG_NAME, "div")

        for post in posts:
            try:
                text = post.text

                if (
                    today_date_str_space in text
                    or today_date_str_nospace in text
                ):
                    img = post.find_element(By.TAG_NAME, "img")
                    src = img.get_attribute("src")

                    if src and "k.kakaocdn.net/dn/" in src:
                        return src

            except Exception:
                continue

        imgs = driver.find_elements(By.TAG_NAME, "img")

        for img in imgs:
            try:
                src = img.get_attribute("src")

                if src and "k.kakaocdn.net/dn/" in src:
                    return src

            except Exception:
                continue

        return None

    except Exception as e:
        print(f"  -> [온정찬] 오류 : {e}")
        return None


# ==========================================================
# 6. 카카오 - 런치투게더
# ==========================================================

def get_kakao_profile_image(driver, url, store_name):
    print(f"  -> [{store_name}] 카카오 프로필 이미지 접근")

    try:
        driver.get(url)
        time.sleep(4)

        imgs = driver.find_elements(By.TAG_NAME, "img")
        kakao_imgs = []

        for img in imgs:
            try:
                src = img.get_attribute("src")

                if not src or "k.kakaocdn.net/dn/" not in src:
                    continue

                size = img.size

                kakao_imgs.append({
                    "element": img,
                    "src": src,
                    "width": size["width"],
                    "height": size["height"],
                })

            except Exception:
                continue

        if not kakao_imgs:
            return None

        profile_candidates = [
            item for item in kakao_imgs
            if (
                item["width"] <= 250
                and item["height"] <= 250
                and item["width"] > 0
            )
        ]

        if not profile_candidates:
            profile_candidates = [kakao_imgs[0]]

        profile = profile_candidates[0]
        profile_element = profile["element"]

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});",
                profile_element
            )
            time.sleep(0.5)
            driver.execute_script(
                "arguments[0].click();",
                profile_element
            )

        except Exception:
            try:
                profile_element.click()
            except Exception:
                pass

        time.sleep(2)

        modal_imgs = driver.find_elements(By.TAG_NAME, "img")
        modal_candidates = []

        for img in modal_imgs:
            try:
                src = img.get_attribute("src")

                if not src or "k.kakaocdn.net/dn/" not in src:
                    continue

                size = img.size
                width = size["width"]
                height = size["height"]

                if width < 150 or height < 150:
                    continue

                modal_candidates.append({
                    "src": src,
                    "width": width,
                    "height": height,
                    "area": width * height,
                })

            except Exception:
                continue

        if modal_candidates:
            modal_candidates.sort(
                key=lambda x: x["area"],
                reverse=True
            )
            return modal_candidates[0]["src"]

        return profile["src"]

    except Exception as e:
        print(f"  -> [{store_name}] 카카오 오류 : {e}")
        return None


# ==========================================================
# 7. 카카오 - 밥심
# ==========================================================

def get_kakao_first_image(driver, url, store_name):
    print(f"  -> [{store_name}] 카카오 최신 메뉴 이미지 수집")

    try:
        driver.get(url)
        time.sleep(3)

        imgs = driver.find_elements(By.TAG_NAME, "img")
        valid_candidates = []

        for img in imgs:
            try:
                src = img.get_attribute("src")

                if not src or "k.kakaocdn.net/dn/" not in src:
                    continue

                size = img.size
                width = size.get("width", 0)
                height = size.get("height", 0)

                if (
                    width > 250
                    or height > 250
                    or (width == 0 and height == 0)
                ):
                    valid_candidates.append(src)

            except Exception:
                continue

        if valid_candidates:
            return valid_candidates[0]

        for img in imgs:
            try:
                src = img.get_attribute("src")

                if src and "k.kakaocdn.net/dn/" in src:
                    return src

            except Exception:
                continue

        return None

    except Exception as e:
        print(f"  -> [{store_name}] 카카오 오류 : {e}")
        return None


# ==========================================================
# 8. Instagram - 런치타임
# ==========================================================

def find_instagram_post_links(driver, profile_url):
    driver.get(profile_url)
    time.sleep(5)

    links = []

    for a in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = a.get_attribute("href")

            if not href:
                continue

            if "/p/" in href or "/reel/" in href:
                clean = href.split("?")[0]

                if clean not in links:
                    links.append(clean)

        except Exception:
            continue

    return links


def parse_instagram_post_date(body_text):
    pattern = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")

    for line in body_text.splitlines():
        match = pattern.fullmatch(line.strip())

        if match:
            return int(match.group(1)), int(match.group(2))

    match = pattern.search(body_text)

    if match:
        return int(match.group(1)), int(match.group(2))

    return None


def extract_instagram_menu_from_post(body_text):
    lines = [
        line.strip().replace("\\", "")
        for line in body_text.splitlines()
    ]

    lines = [line for line in lines if line]

    date_index = -1

    date_pattern = re.compile(
        rf"{today.month}월\s*{today.day}일"
    )

    for i, line in enumerate(lines):
        if date_pattern.search(line):
            date_index = i
            break

    if date_index == -1:
        return None

    menu_lines = []

    ignored = {
        "로그인",
        "가입하기",
        "팔로우",
        "팔로우하기",
        "Follow",
        "Following",
        "Threads",
        "Instagram",
        "홈",
        "Home",
        "좋아요",
        "댓글",
        "공유",
        "보내기",
        "저장",
        "번역",
        "Translate",
        "더 보기",
        "More",
    }

    for line in lines[date_index + 1:]:
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            break

        if line in ignored:
            continue

        if line == "lunchtime_ypp":
            continue

        if "팔로워" in line or "followers" in line:
            continue

        if re.fullmatch(r"\d+\s*(초|분|시간|일|주|개월|년)\s*전", line):
            continue

        if re.fullmatch(r"\d+\s*[smhdw]", line, re.IGNORECASE):
            continue

        if line.isdigit():
            continue

        menu_lines.append(line)

    if not menu_lines:
        return None

    return menu_lines


def get_instagram_menu(driver, profile_url):
    print("  -> [런치타임] Instagram 게시글 메뉴 수집 중")

    try:
        links = find_instagram_post_links(
            driver,
            profile_url
        )

        print(f"     Instagram 게시글 링크 {len(links)}개 발견")

        for post_url in links[:10]:

            try:
                driver.get(post_url)
                time.sleep(3)

                body_text = driver.find_element(
                    By.TAG_NAME,
                    "body"
                ).text

                post_date = parse_instagram_post_date(
                    body_text
                )

                if not post_date:
                    continue

                post_month, post_day = post_date

                if (
                    post_month != today.month
                    or post_day != today.day
                ):
                    continue

                menu_lines = extract_instagram_menu_from_post(
                    body_text
                )

                if menu_lines:
                    print("     Instagram 오늘 게시글 발견")
                    print("     메뉴:")
                    for menu in menu_lines:
                        print(f"       - {menu}")

                    return {
                        "source": "instagram",
                        "source_url": post_url,
                        "menu_lines": menu_lines,
                    }

            except Exception as e:
                print(f"     게시글 확인 실패: {e}")
                continue

        print("     오늘 Instagram 게시글을 찾지 못했습니다.")

    except Exception as e:
        print(f"  -> [런치타임] Instagram 오류 : {e}")

    return None


# ==========================================================
# 9. Threads - 런치타임 fallback
# ==========================================================

def get_threads_menu(driver, url):
    print("  -> [런치타임] Threads fallback 수집 중")

    try:
        driver.get(url)
        time.sleep(4)

        body_text = driver.find_element(
            By.TAG_NAME,
            "body"
        ).text

        lines = body_text.split("\n")

        target_date1 = today_date_str_nospace
        target_date2 = today_date_str_space

        start_idx = -1

        for i, line in enumerate(lines):
            line_clean = line.strip()

            if (
                target_date1 in line_clean
                or target_date2 in line_clean
            ):
                start_idx = i
                break

        if start_idx == -1:
            return None

        filtered_lines = []

        ignored = {
            "스레드",
            "답글",
            "미디어",
            "리포스트",
            "팔로우",
            "언급",
            "로그인",
            "가입하기",
            "lunchtime_ypp",
            "Home",
            "Follow",
            "Mention",
            "Threads",
            "Replies",
            "Media",
            "Reposts",
            "Translate",
        }

        for line in lines[start_idx + 1:]:
            line = line.strip().replace("\\", "")

            if not line:
                continue

            if (
                "월" in line
                and "일" in line
                and target_date1 not in line
                and target_date2 not in line
            ):
                break

            if line in ignored:
                continue

            if (
                "팔로워" in line
                or "followers" in line
                or "시간 전" in line
                or "일 전" in line
                or line.endswith("h")
                or line.endswith("d")
                or line.isdigit()
            ):
                continue

            filtered_lines.append(line)

        if not filtered_lines:
            return None

        for i, line in enumerate(filtered_lines):
            if line.startswith("#"):
                filtered_lines = filtered_lines[:i]
                break

        if not filtered_lines:
            return None

        return {
            "source": "threads",
            "source_url": url,
            "menu_lines": filtered_lines,
        }

    except Exception as e:
        print(f"  -> [런치타임] Threads 오류 : {e}")
        return None


# ==========================================================
# 10. 메뉴 HTML 생성
# ==========================================================

def menu_lines_to_html(menu_lines):
    if not menu_lines:
        return "<div>오늘의 메뉴를 찾지 못했습니다.</div>"

    safe_lines = []

    for line in menu_lines:
        line = (
            line.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
        )

        safe_lines.append(
            f'<div class="menu-line">{line}</div>'
        )

    return """
    <div class="text-menu">
        %s
    </div>
    """ % "\n".join(safe_lines)


# ==========================================================
# 10-2. Gemini Vision - 메뉴판 이미지에서 텍스트만 추출
# ==========================================================

def download_image_bytes(url):
    """카카오 CDN 등에 올라온 이미지를 다운로드해서 (bytes, mime_type)으로 반환"""
    try:
        res = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if res.status_code == 200 and res.content:
            mime_type = res.headers.get("Content-Type", "image/jpeg").split(";")[0]
            return res.content, mime_type
    except Exception as e:
        print(f"     이미지 다운로드 실패: {e}")
    return None, None


def build_image_extraction_prompt(restaurant_name):
    return (
        f"이 이미지는 한국 '{restaurant_name}' 식당의 점심 메뉴판입니다. "
        f"오늘은 {today.year}년 {today_date_str_space} {today_weekday}요일입니다. "
        "이미지 제목이나 상단에 특정 날짜(예: '9월 23일')가 적혀있다면, 그 날짜가 오늘과 "
        "일치하는지 확인하세요. 날짜가 아예 안 적혀 있고 요일별 표만 있는 이미지라면 "
        "(이미 오늘 요일에 해당하는 부분만 잘려서 온 이미지라고 가정하고) 오늘 것으로 간주하세요. "
        "이미지에 적힌 날짜가 오늘과 다르면 is_today를 false로, 맞거나 날짜 표기가 없으면 "
        "true로 답하세요. "
        "items에는 오늘 날짜(요일)에 해당하는 메뉴 항목만, 각 항목마다 category를 "
        "main(메인 요리/특선), soup(국/찌개/탕), side(반찬/나물/볶음), "
        "kimchi(김치/깍두기/장아찌), snack(간식/과자/후식/빵), drink(음료/차/커피/밥) 중 "
        "하나로 분류해서 main→soup→side→kimchi→snack→drink 순으로 정렬해 담아주세요. "
        "다른 설명 없이 JSON 객체 하나만 답변하세요. "
        '형식: {"is_today":true,"items":[{"name":"오징어김치볶음밥","category":"main"}]}. '
        "메뉴를 읽을 수 없으면 items를 빈 배열 []로 반환하세요."
    )


def parse_ai_extraction_response(text, restaurant_name, provider_label):
    """
    {"is_today":bool,"items":[...]} 형태의 응답을 검증한다.
    is_today가 false면(=이미지가 오늘 날짜가 아니면) None을 반환해서
    이 결과를 절대 신뢰/캐시하지 않게 한다.
    """
    try:
        data = _parse_ai_json(text)
    except Exception as e:
        print(f"     → [{restaurant_name}] {provider_label} 응답 파싱 실패: {e}")
        return None

    if isinstance(data, dict):
        items = data.get("items")
        is_today = data.get("is_today", True)
    else:
        # 혹시 예전 방식(배열만)으로 답하면 그대로 사용
        items = data
        is_today = True

    if is_today is False:
        print(f"     → [{restaurant_name}] {provider_label}: 오늘 날짜 메뉴가 아닌 것으로 판단 (오래된 게시물)")
        return None

    menu_items = _normalize_menu_items(items) if isinstance(items, list) else []

    if not menu_items:
        print(f"     → [{restaurant_name}] {provider_label}가 메뉴를 못 읽음 (빈 결과)")
        return None

    garbled = [it["name"] for it in menu_items if _looks_garbled(it["name"])]
    if len(garbled) >= 2:
        print(f"     → [{restaurant_name}] {provider_label} 결과에 깨진 글자 감지, 거부: {garbled}")
        return None

    return menu_items


_gemini_model_list_cache = None


def list_gemini_models():
    """
    이 API 키로 지금 실제 쓸 수 있는 flash 계열(이미지 입력 가능) 모델 목록을 가져온다.
    무료 할당량은 모델별로 따로 매겨지므로, 여러 모델을 순서대로 시도하면
    OpenRouter로 넘어가기 전에 Gemini만으로 쓸 수 있는 총량이 늘어난다.
    실행당 한 번만 조회해서 캐시해둔다.
    """
    global _gemini_model_list_cache
    if _gemini_model_list_cache is not None:
        return _gemini_model_list_cache

    candidates = [GEMINI_MODEL]

    if GEMINI_API_KEY:
        try:
            res = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}",
                timeout=15,
            )
            for m in res.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                if "flash" not in name.lower():
                    continue
                # tts/embedding/live/image 전용 등은 이미지+텍스트 메뉴 추출에 못 쓰는 변종 모델
                if any(bad in name.lower() for bad in ("tts", "embedding", "live", "image-generation", "native-audio")):
                    continue
                if name not in candidates:
                    candidates.append(name)
        except Exception as e:
            print(f"     (Gemini 모델 목록 조회 실패, 기본 모델만 사용: {e})")

    _gemini_model_list_cache = candidates[:5]  # 무한정 시도하지 않도록 상한
    print(f"     Gemini 시도 순서: {_gemini_model_list_cache}")
    return _gemini_model_list_cache


def extract_menu_via_gemini(image_bytes, mime_type, restaurant_name):
    """
    메뉴판 사진을 Gemini에게 보내서, 오늘 날짜에 해당하는 메뉴를
    {"name": 메뉴명, "category": 분류} 목록으로 뽑아온다.
    이미지 자체가 오늘 게시물이 아니라고 판단되면 None을 반환한다.
    여러 Gemini 모델을 순서대로 시도해보고, 전부 실패하면 None을 반환한다
    (호출하는 쪽에서 OpenRouter나 원본 이미지로 폴백한다).
    """
    if not GEMINI_API_KEY or not image_bytes:
        return None

    prompt = build_image_extraction_prompt(restaurant_name)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    for model_name in list_gemini_models():
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={GEMINI_API_KEY}",
                json={
                    "contents": [{
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": mime_type, "data": b64}},
                        ]
                    }],
                    "generationConfig": {
                        "temperature": 0,
                        "response_mime_type": "application/json",
                    },
                },
                timeout=18,
            )

            data = res.json()

            if "candidates" not in data:
                print(f"     → [{restaurant_name}] Gemini({model_name}) 응답 이상 (HTTP {res.status_code}): {data}")
                continue

            text = data["candidates"][0]["content"]["parts"][0]["text"]
            menu_items = parse_ai_extraction_response(text, restaurant_name, f"Gemini({model_name})")

            if menu_items:
                print(f"     → [{restaurant_name}] Gemini({model_name}) 메뉴 추출 성공 ({len(menu_items)}개 항목)")
                return menu_items

        except Exception as e:
            print(f"     → [{restaurant_name}] Gemini({model_name}) 추출 실패: {e}")

    return None


def _parse_ai_json(text):
    """LLM 응답에서 JSON 배열만 뽑아 파싱한다 (```json 코드펜스가 섞여 와도 처리)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _looks_garbled(name):
    """
    한글 사이/옆에 소문자 영어가 바로 붙어 나오면 OCR이 깨졌을 가능성이 높다고 본다.
    (BBQ, ICE처럼 대문자 약어는 정상적인 메뉴 표기라 걸러내지 않음)
    """
    for m in re.finditer(r"[A-Za-z]+", name):
        run = m.group()
        if sum(1 for c in run if c.islower()) < 1:
            continue
        start, end = m.span()
        before = name[start - 1] if start > 0 else ""
        after = name[end] if end < len(name) else ""
        if re.match(r"[가-힣]", before) or re.match(r"[가-힣]", after):
            return True
    return False


def _normalize_menu_items(items):
    valid_categories = {"main", "soup", "side", "kimchi", "snack", "drink"}
    menu_items = []

    for entry in items:
        if isinstance(entry, dict) and str(entry.get("name", "")).strip():
            category = entry.get("category")
            category = category if category in valid_categories else "side"
            menu_items.append({
                "name": str(entry["name"]).strip(),
                "category": category,
            })
        elif isinstance(entry, str) and entry.strip():
            menu_items.append({"name": entry.strip(), "category": "side"})

    return menu_items


def extract_menu_via_openrouter(image_bytes, mime_type, restaurant_name):
    """Gemini가 실패했을 때의 보험용 폴백. OpenRouter의 무료 비전 모델로 재시도한다."""
    if not OPENROUTER_API_KEY or not image_bytes:
        return None

    prompt = build_image_extraction_prompt(restaurant_name)

    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime_type};base64,{b64}"
                        }},
                    ],
                }],
            },
            timeout=18,
        )

        data = res.json()

        if "choices" not in data:
            print(f"     → [{restaurant_name}] OpenRouter 응답 이상 (HTTP {res.status_code}): {data}")
            return None

        text = data["choices"][0]["message"]["content"]
        menu_items = parse_ai_extraction_response(text, restaurant_name, "OpenRouter")

        if menu_items:
            print(f"     → [{restaurant_name}] OpenRouter 메뉴 추출 성공 ({len(menu_items)}개 항목)")

        return menu_items

    except Exception as e:
        print(f"     → [{restaurant_name}] OpenRouter 추출 실패: {e}")
        return None


def extract_menu_via_ai(image_bytes, mime_type, restaurant_name):
    """
    이미지에서 메뉴를 뽑는 통합 진입점.
    Gemini 우선(할당량/내용 중복 체크 포함) → 실패하면 OpenRouter 무료 모델로 보험 시도.
    반환: (menu_items 또는 None, 성공한 소스 이름 또는 None)
    """
    if not image_bytes:
        return None, None

    h = content_hash(image_bytes)

    if should_call_gemini(restaurant_name, h):
        items = extract_menu_via_gemini(image_bytes, mime_type, restaurant_name)
        record_gemini_attempt(restaurant_name, h, bool(items))
        if items:
            return items, "gemini_ocr"

    items = extract_menu_via_openrouter(image_bytes, mime_type, restaurant_name)
    if items:
        return items, "openrouter_ocr"

    return None, None


def menu_items_to_html(menu_items):
    """카테고리별로 색을 입혀서 메뉴 목록 HTML을 만든다."""
    if not menu_items:
        return "<div>오늘의 메뉴를 찾지 못했습니다.</div>"

    safe_lines = []

    for item in menu_items:
        name = (
            item["name"].replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
        )
        category = item.get("category", "side")

        safe_lines.append(
            f'<div class="menu-line cat-{category}">{name}</div>'
        )

    return """
    <div class="text-menu">
        %s
    </div>
    """ % "\n".join(safe_lines)


def classify_menu_lines_via_gemini(menu_lines, restaurant_name):
    """
    이미 텍스트로 확보된 메뉴 줄(예: Threads/Instagram 캡션)을
    Gemini에게 다시 보내서 카테고리만 분류받는다. 이미지가 없으므로
    텍스트 프롬프트만 보내고, 여러 모델을 순서대로 시도한다.
    """
    if not GEMINI_API_KEY or not menu_lines:
        return None

    prompt = (
        f"다음은 한국 '{restaurant_name}' 식당의 오늘 점심 메뉴를 줄 단위로 나열한 것입니다. "
        "각 줄을 아래 카테고리 중 하나로 분류하고, 순서는 원래 순서를 최대한 유지해주세요. "
        "카테고리: main(메인 요리/특선), soup(국/찌개/탕), side(반찬/나물/볶음), "
        "kimchi(김치/깍두기/장아찌), snack(간식/과자/후식/빵), drink(음료/차/커피/밥/라면). "
        "메뉴가 아닌 광고 문구나 해시태그, 이모지만 있는 줄은 제외하세요. "
        "다른 설명 없이 JSON 배열만 답변하세요. "
        '형식: [{"name":"콩나물국","category":"soup"}, {"name":"바싹불고기","category":"main"}]\n\n'
        "메뉴 목록:\n" + "\n".join(menu_lines)
    )

    for model_name in list_gemini_models():
        try:
            res = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={GEMINI_API_KEY}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "response_mime_type": "application/json",
                    },
                },
                timeout=18,
            )

            data = res.json()

            if "candidates" not in data:
                print(f"     → [{restaurant_name}] Gemini({model_name}) 분류 실패 (HTTP {res.status_code}): {data}")
                continue

            text = data["candidates"][0]["content"]["parts"][0]["text"]
            items = json.loads(text)
            menu_items = _normalize_menu_items(items) if isinstance(items, list) else []

            if not menu_items:
                continue

            garbled = [it["name"] for it in menu_items if _looks_garbled(it["name"])]
            if len(garbled) >= 2:
                print(f"     → [{restaurant_name}] Gemini({model_name}) 분류 결과에 깨진 글자 감지, 거부: {garbled}")
                continue

            print(f"     → [{restaurant_name}] Gemini({model_name}) 텍스트 분류 성공 ({len(menu_items)}개 항목)")
            return menu_items

        except Exception as e:
            print(f"     → [{restaurant_name}] Gemini({model_name}) 분류 오류: {e}")

    return None


def classify_menu_lines_via_openrouter(menu_lines, restaurant_name):
    """Gemini 텍스트 분류가 실패했을 때의 보험용 폴백."""
    if not OPENROUTER_API_KEY or not menu_lines:
        return None

    prompt = (
        f"다음은 한국 '{restaurant_name}' 식당의 오늘 점심 메뉴를 줄 단위로 나열한 것입니다. "
        "각 줄을 main(메인 요리/특선), soup(국/찌개/탕), side(반찬/나물/볶음), "
        "kimchi(김치/깍두기/장아찌), snack(간식/과자/후식/빵), drink(음료/차/커피/밥/라면) "
        "중 하나로 분류하고, 원래 순서를 최대한 유지해주세요. 메뉴가 아닌 광고 문구, "
        "해시태그, 이모지만 있는 줄은 제외하세요. 다른 설명 없이 JSON 배열만 답변하세요. "
        '형식: [{"name":"콩나물국","category":"soup"}]\n\n'
        "메뉴 목록:\n" + "\n".join(menu_lines)
    )

    try:
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=18,
        )

        data = res.json()

        if "choices" not in data:
            print(f"     → [{restaurant_name}] OpenRouter 분류 실패 (HTTP {res.status_code}): {data}")
            return None

        text = data["choices"][0]["message"]["content"]
        items = _parse_ai_json(text)
        menu_items = _normalize_menu_items(items) if isinstance(items, list) else []

        if not menu_items:
            return None

        print(f"     → [{restaurant_name}] OpenRouter 텍스트 분류 성공 ({len(menu_items)}개 항목)")
        return menu_items

    except Exception as e:
        print(f"     → [{restaurant_name}] OpenRouter 분류 오류: {e}")
        return None


def classify_menu_lines_via_ai(menu_lines, restaurant_name):
    """텍스트 분류 통합 진입점. Gemini 우선 → 실패하면 OpenRouter 보험 시도."""
    if not menu_lines:
        return None

    h = content_hash("\n".join(menu_lines))

    if should_call_gemini(restaurant_name, h):
        items = classify_menu_lines_via_gemini(menu_lines, restaurant_name)
        record_gemini_attempt(restaurant_name, h, bool(items))
        if items:
            return items

    return classify_menu_lines_via_openrouter(menu_lines, restaurant_name)


_CATEGORY_KEYWORDS = [
    ("kimchi", ["김치", "깍두기", "장아찌", "겉절이"]),
    ("soup", ["국", "찌개", "탕", "장국", "전골", "라면", "우동", "칼국수"]),
    ("drink", ["음료", "차", "커피", "콜라", "사이다", "식혜", "숭늉", "주스", "우유", "아메리카노"]),
    ("snack", ["후식", "디저트", "빵", "과자", "요거트", "시리얼", "토스트", "아이스크림", "케이크", "쿠키"]),
]


def classify_menu_lines_locally(menu_lines):
    """
    AI를 거치지 않고 키워드 규칙만으로 분류한다. 원문 글자를 단 하나도 바꾸지 않아서
    (이미 정확한 텍스트인) Threads/Instagram 캡션을 AI가 잘못 다시 쓰는 위험이 없다.
    """
    menu_items = []

    for i, line in enumerate(menu_lines):
        category = "main" if i == 0 else "side"

        for cat, keywords in _CATEGORY_KEYWORDS:
            if any(k in line for k in keywords):
                category = cat
                break

        menu_items.append({"name": line, "category": category})

    return menu_items


geolocator = Nominatim(user_agent="gasan_lunch_map_new")
geocode_cache = {}

def get_coords(address):
    if address in geocode_cache:
        return geocode_cache[address]
    try:
        loc = geolocator.geocode(address, timeout=10)
        if loc:
            coords = (float(loc.latitude), float(loc.longitude))
            geocode_cache[address] = coords
            return coords
    except Exception as e:
        print(f"  -> 주소 좌표 변환 실패: {address} / {e}")
    
    fallback = (37.471364252495015, 126.88404214632791) # 실패 시 기준좌표(회사)
    geocode_cache[address] = fallback
    return fallback

# 회사 위치 마커 겹침 방지를 위해 요청하신 좌표로 분리
office_coords = (37.471364252495015, 126.88404214632791)

def calculate_walking_info(dest_coords):
    try:
        dist_meters = geodesic(
            office_coords,
            dest_coords
        ).meters

        walk_minutes = round(
            dist_meters / 70
        )

        walk_minutes = max(
            1,
            walk_minutes
        )

        return int(dist_meters), walk_minutes

    except Exception:
        return 0, 0


# ==========================================================
# 11-2. 카카오 도보 경로 조회 (회사 ↔ 각 식당, 결과는 캐시해서 재사용)
# ==========================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)

routes_cache = {}
if os.path.exists(ROUTES_CACHE_JSON):
    try:
        with open(ROUTES_CACHE_JSON, "r", encoding="utf-8") as f:
            routes_cache = json.load(f)
    except Exception:
        routes_cache = {}

gemini_attempts = {}
if os.path.exists(GEMINI_ATTEMPTS_JSON):
    try:
        with open(GEMINI_ATTEMPTS_JSON, "r", encoding="utf-8") as f:
            gemini_attempts = json.load(f)
    except Exception:
        gemini_attempts = {}


def content_hash(data):
    """이미지 bytes 또는 텍스트를 짧은 지문으로 바꾼다 (내용이 바뀌었는지 비교용)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def should_call_gemini(restaurant_name, current_hash):
    """
    오늘 이미 이 정확한 내용(해시)으로 Gemini를 불렀다가 실패했다면,
    새 게시물이 올라오기 전까지는 다시 불러도 결과가 같으므로 호출을 건너뛴다.
    """
    entry = gemini_attempts.get(restaurant_name)
    today_str = today.strftime("%Y-%m-%d")

    if (
        entry
        and entry.get("date") == today_str
        and entry.get("hash") == current_hash
        and not entry.get("succeeded")
    ):
        print(f"     → [{restaurant_name}] 지난번과 같은 내용, Gemini 재호출 생략")
        return False

    return True


def record_gemini_attempt(restaurant_name, current_hash, succeeded):
    gemini_attempts[restaurant_name] = {
        "date": today.strftime("%Y-%m-%d"),
        "hash": current_hash,
        "succeeded": succeeded,
    }


def get_walking_route(dest_coords, cache_key):
    """
    회사(office_coords) -> dest_coords 도보 경로를 카카오 도보 경로 조회 API로 가져온다.
    같은 식당 좌표는 항상 같은 경로이므로, 한 번 성공하면 routes_cache.json에 저장해두고
    다음부터는 API를 다시 호출하지 않는다 (무료 쿼터 절약).
    """
    cached = routes_cache.get(cache_key)
    if cached and cached.get("points"):
        return cached

    if not KAKAO_REST_KEY:
        return None

    try:
        res = requests.get(
            "https://dapi.kakao.com/v2/routing/walk",
            headers={"Authorization": f"KakaoAK {KAKAO_REST_KEY}"},
            params={
                "start_x": office_coords[1],
                "start_y": office_coords[0],
                "end_x": dest_coords[1],
                "end_y": dest_coords[0],
            },
            timeout=10,
        )
        data = res.json()

        if data.get("status") != "OK":
            print(f"     → [{cache_key}] 도보 경로 조회 실패: {data.get('status')}")
            return None

        route = data["route"]
        points = []
        for leg in route.get("legs", []):
            for step in leg.get("steps", []):
                for x, y in step.get("path", {}).get("points", []):
                    points.append({"lat": y, "lng": x})

        result = {
            "points": points,
            "distance": route["properties"].get("totalDistance"),
            "time_sec": route["properties"].get("totalTime"),
        }
        routes_cache[cache_key] = result
        print(f"     → [{cache_key}] 도보 경로 {len(points)}개 좌표 수신")
        return result

    except Exception as e:
        print(f"     → [{cache_key}] 도보 경로 조회 오류: {e}")
        return None


# ==========================================================
# 12. 실제 데이터 수집
# ==========================================================

os.makedirs(
    OUTPUT_DIR,
    exist_ok=True
)

previous_data = {}
if os.path.exists(OUTPUT_JSON):
    try:
        with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
            previous_data = json.load(f)
    except Exception:
        previous_data = {}

previous_restaurants = {
    item.get("name"): item
    for item in previous_data.get("restaurants", [])
}

driver = None
scraped_data = []

try:
    driver = create_driver()

    print()
    print("=" * 60)
    print("자동 수집 시작")
    print("=" * 60)

    for item in cafeteria_list:

        print()
        print(f"[{item['name']}] 정보 수집 중...")

        # exact_lat, exact_lng 값이 리스트에 있으면 그 좌표를 무조건 사용
        if "exact_lat" in item and "exact_lng" in item:
            lat = item["exact_lat"]
            lng = item["exact_lng"]
        else:
            base_lat, base_lng = get_coords(item["address"])
            lat = base_lat + item.get("lat_offset", 0)
            lng = base_lng + item.get("lng_offset", 0)

        dist, walk_min = calculate_walking_info(
            (lat, lng)
        )

        # 카카오 도보 경로 API로 실제 걷는 경로(선)와, 가능하면 더 정확한 거리/시간을 받아온다
        route_info = get_walking_route((lat, lng), item["name"])
        route_points = route_info["points"] if route_info else []

        if route_info and route_info.get("distance"):
            dist = route_info["distance"]
        if route_info and route_info.get("time_sec"):
            walk_min = max(1, round(route_info["time_sec"] / 60))

        html_content = ""
        source = ""
        raw_ref = None  # OCR 돌리기 전 원본(이미지 데이터/URL 또는 원본 게시물 링크)

        # 오늘 이미 Gemini로 성공 추출한 식당은 다시 이미지/API 호출 없이 그대로 재사용
        # (무료 할당량이 하루 20회로 빠듯해서, 성공한 건 그날 하루 캐시해서 아낀다)
        today_str = today.strftime("%Y-%m-%d")
        cached_previous = previous_restaurants.get(item["name"])
        cache_hit = bool(
            cached_previous
            and cached_previous.get("menu_date") == today_str
            and cached_previous.get("source") in ("gemini_ocr", "openrouter_ocr")
            and cached_previous.get("html")
            and cached_previous.get("pipeline_version") == EXTRACTION_PIPELINE_VERSION
        )

        checked_at = None

        if cache_hit:
            print(f"     → [{item['name']}] 오늘 이미 {cached_previous['source']} 추출 성공, 재사용 (API 호출 생략)")
            html_content = cached_previous["html"]
            source = cached_previous["source"]
            raw_ref = cached_previous.get("raw_ref")
            checked_at = cached_previous.get("checked_at")

        elif item["type"] == "ojeong":

            src, ocr_bytes = crop_ojeong_by_weekday(
                item["url"]
            )

            if src:
                raw_ref = {"type": "image", "url": src}

                menu_lines, ocr_source = extract_menu_via_ai(
                    ocr_bytes, "image/jpeg", item["name"]
                )

                if menu_lines:
                    html_content = menu_items_to_html(menu_lines)
                    source = ocr_source
                else:
                    html_content = f"""
                    <img
                        src="{src}"
                        class="menu-image"
                        alt="오정 오늘의 메뉴"
                    >
                    """
                    source = "local_image"
            else:
                html_content = """
                <div class="error-menu">
                    오정 메뉴를 불러오지 못했습니다.
                </div>
                """

        elif item["type"] == "kakao_posts":

            img_src = get_kakao_posts_image(
                driver,
                item["url"]
            )

            if img_src:
                raw_ref = {"type": "image", "url": img_src}
                img_bytes, img_mime = download_image_bytes(img_src)
                menu_lines, ocr_source = (
                    extract_menu_via_ai(img_bytes, img_mime, item["name"])
                    if img_bytes else (None, None)
                )

                if menu_lines:
                    html_content = menu_items_to_html(menu_lines)
                    source = ocr_source
                else:
                    html_content = f"""
                    <img
                        src="{img_src}"
                        class="menu-image"
                        alt="온정찬 오늘의 메뉴"
                    >
                    """
                    source = "kakao_posts"
            else:
                html_content = """
                <div class="error-menu">
                    온정찬 메뉴 이미지를 찾지 못했습니다.
                </div>
                """

        elif item["type"] == "kakao_profile":

            img_src = get_kakao_profile_image(
                driver,
                item["url"],
                item["name"]
            )

            if img_src:
                raw_ref = {"type": "image", "url": img_src}
                img_bytes, img_mime = download_image_bytes(img_src)
                menu_lines, ocr_source = (
                    extract_menu_via_ai(img_bytes, img_mime, item["name"])
                    if img_bytes else (None, None)
                )

                if menu_lines:
                    html_content = menu_items_to_html(menu_lines)
                    source = ocr_source
                else:
                    html_content = f"""
                    <img
                        src="{img_src}"
                        class="menu-image"
                        alt="런치투게더 오늘의 메뉴"
                    >
                    """
                    source = "kakao_profile"
            else:
                html_content = """
                <div class="error-menu">
                    카카오 메뉴 이미지를 찾지 못했습니다.
                </div>
                """

        elif item["type"] == "instagram_threads":

            result = get_instagram_menu(
                driver,
                item["instagram_url"]
            )

            if result:

                raw_ref = {"type": "link", "url": result.get("source_url")}
                menu_items = classify_menu_lines_locally(result["menu_lines"])

                if menu_items:
                    html_content = menu_items_to_html(menu_items)
                else:
                    html_content = menu_lines_to_html(
                        result["menu_lines"]
                    )

                source = result["source"]

            else:

                print(
                    "     → Instagram 실패. Threads로 전환합니다."
                )

                result = get_threads_menu(
                    driver,
                    item["threads_url"]
                )

                if result:

                    raw_ref = {"type": "link", "url": result.get("source_url")}
                    menu_items = classify_menu_lines_locally(result["menu_lines"])

                    if menu_items:
                        html_content = menu_items_to_html(menu_items)
                    else:
                        html_content = menu_lines_to_html(
                            result["menu_lines"]
                        )

                    source = result["source"]

                else:

                    html_content = """
                    <div class="error-menu">
                        오늘의 런치타임 메뉴를 찾지 못했습니다.
                    </div>
                    """

                    source = "none"

        elif item["type"] == "kakao_first":

            img_src = get_kakao_first_image(
                driver,
                item["url"],
                item["name"]
            )

            if img_src:
                raw_ref = {"type": "image", "url": img_src}
                img_bytes, img_mime = download_image_bytes(img_src)
                menu_lines, ocr_source = (
                    extract_menu_via_ai(img_bytes, img_mime, item["name"])
                    if img_bytes else (None, None)
                )

                if menu_lines:
                    html_content = menu_items_to_html(menu_lines)
                    source = ocr_source
                else:
                    html_content = f"""
                    <img
                        src="{img_src}"
                        class="menu-image"
                        alt="밥심 오늘의 메뉴"
                    >
                    """
                    source = "kakao_first"
            else:
                html_content = """
                <div class="error-menu">
                    카카오 메뉴 이미지를 찾지 못했습니다.
                </div>
                """

        previous = previous_restaurants.get(item["name"])
        menu_status = "today"

        failed_this_run = (
            not html_content
            or source == "none"
            or "찾지 못했습니다" in html_content
            or "불러오지 못했습니다" in html_content
        )

        if (
            failed_this_run
            and previous
            and previous_data.get("date") == today.strftime("%Y-%m-%d")
            and previous.get("html")
            and previous.get("source") not in ("none", "")
        ):
            html_content = previous["html"]
            source = previous["source"]
            raw_ref = previous.get("raw_ref")
            menu_status = "preserved_from_previous_run"
            checked_at = previous.get("checked_at")
            print(f"     → [{item['name']}] 이전 정상 수집 메뉴 유지")

        elif failed_this_run:
            menu_status = "missing"
            checked_at = None

        elif checked_at is None:
            # 이번 실행에서 새로 확인된 정상 메뉴
            checked_at = today.strftime("%H:%M")

        scraped_data.append({
            "name": item["name"],
            "address": item["address"],
            "lat": lat,
            "lng": lng,
            "dist": dist,
            "walk_min": walk_min,
            "route": route_points,
            "source": source,
            "html": html_content,
            "raw_ref": raw_ref,
            "menu_status": menu_status,
            "menu_date": today.strftime("%Y-%m-%d") if menu_status != "missing" else None,
            "checked_at": checked_at,
            "pipeline_version": EXTRACTION_PIPELINE_VERSION,
        })

        time.sleep(1.5)

finally:

    if driver:
        driver.quit()


# ==========================================================
# 13. menu.json 저장
# ==========================================================

result = {
    "updated_at": today.strftime(
        "%Y-%m-%d %H:%M:%S"
    ),
    "updated_at_display": today.strftime(
        "%Y.%m.%d %H:%M"
    ),
    "date": today.strftime(
        "%Y-%m-%d"
    ),
    "date_display": today_date_str_space,
    "weekday": today_weekday,
    "office": {
        "address": OFFICE_ADDRESS,
        "lat": office_coords[0],
        "lng": office_coords[1],
    },
    "restaurants": scraped_data,
}


with open(
    OUTPUT_JSON,
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        result,
        f,
        ensure_ascii=False,
        indent=2
    )

# 도보 경로 캐시 저장 (다음 실행부터는 API를 다시 호출하지 않고 재사용)
with open(ROUTES_CACHE_JSON, "w", encoding="utf-8") as f:
    json.dump(routes_cache, f, ensure_ascii=False, indent=2)

# Gemini 시도 기록 저장 (내용이 안 바뀌었으면 다음 실행에서 API 재호출을 건너뛴다)
with open(GEMINI_ATTEMPTS_JSON, "w", encoding="utf-8") as f:
    json.dump(gemini_attempts, f, ensure_ascii=False, indent=2)


print()
print("=" * 60)
print("자동 수집 완료")
print(f"저장 파일 : {OUTPUT_JSON}")
print("=" * 60)
