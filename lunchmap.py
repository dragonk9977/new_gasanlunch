import os
import time
import json
import base64
from io import BytesIO
from datetime import datetime
from PIL import Image
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# ==========================================================
# 1. 사용자 설정
# ==========================================================
OJEONG_IMAGE_PATH = "오정메뉴.jpg"
OFFICE_ADDRESS = "서울 금천구 가산디지털2로 30"

# ==========================================================
# 2. 오늘 날짜 / 요일
# ==========================================================
today = datetime.now()
weekdays = ["월", "화", "수", "목", "금", "토", "일"]
today_weekday_index = today.weekday()
today_weekday = weekdays[today_weekday_index]
today_date_str_space = f"{today.month}월 {today.day}일"
today_date_str_nospace = f"{today.month}월{today.day}일"
ojeong_weekday_index = min(today_weekday_index, 4)

print(f"\n{'='*60}\n오늘 날짜 : {today_date_str_space} ({today_weekday}요일)\n{'='*60}")

# ==========================================================
# 3. 오정 메뉴 (요일별 Crop - 원래 설정인 max_height=420으로 길게 출력)
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
        cropped_img = img.crop((crop_left, top_margin, crop_right, bottom_margin))
        max_height = 420
        if cropped_img.height > max_height:
            ratio = max_height / cropped_img.height
            new_width = int(cropped_img.width * ratio)
            cropped_img = cropped_img.resize((new_width, max_height), Image.LANCZOS)
        buffered = BytesIO()
        cropped_img.save(buffered, format="JPEG", quality=95)
        encoded_string = base64.b64encode(buffered.getvalue()).decode("utf-8")
        print(f" -> [오정] {['월', '화', '수', '목', '금'][ojeong_weekday_index]}요일 메뉴 크롭 완료")
        return "data:image/jpeg;base64," + encoded_string
    except Exception as e:
        print(f" -> [오정] Crop 실패 : {e}")
        return None

# ==========================================================
# 4. 식당 목록 및 위치 설정
# ==========================================================
cafeteria_list = [
    { "name": "오정", "address": "서울 금천구 가산디지털2로 30", "type": "ojeong", "url": OJEONG_IMAGE_PATH, "lat_offset": 0.0000, "lng_offset": -0.0003 },
    { "name": "온정찬", "address": "서울 금천구 가산디지털1로 75-15", "type": "kakao_posts", "url": "https://pf.kakao.com/_UIdXn/posts", "lat_offset": 0.0002, "lng_offset": 0.0002 },
    { "name": "런치투게더", "address": "서울 금천구 가산디지털1로 58", "type": "kakao_profile", "url": "https://pf.kakao.com/_swtYxl", "lat_offset": -0.0002, "lng_offset": 0.0003 },
    { "name": "런치타임", "address": "서울 금천구 가산디지털2로 24", "type": "threads", "url": "https://www.threads.net/@lunchtime_ypp", "lat_offset": -0.0003, "lng_offset": -0.0002 },
    { "name": "밥심", "address": "서울 금천구 가산디지털2로 46", "type": "kakao_first", "url": "https://pf.kakao.com/_mHWxjX", "lat_offset": 0.0003, "lng_offset": -0.0001 }
]

# ==========================================================
# 5. 주소 → 좌표 및 도보 거리 계산
# ==========================================================
geolocator = Nominatim(user_agent="gasan_lunch_map")

def get_coords(address):
    try:
        loc = geolocator.geocode(address)
        if loc:
            return loc.latitude, loc.longitude
    except Exception:
        pass
    return 37.481, 126.882

office_coords = get_coords(OFFICE_ADDRESS)

def calculate_walking_info(dest_coords):
    try:
        dist_meters = geodesic(office_coords, dest_coords).meters
        walk_minutes = round(dist_meters / 70)
        if walk_minutes < 1:
            walk_minutes = 1
        return int(dist_meters), walk_minutes
    except:
        return 0, 0

# ==========================================================
# 6. Selenium 설정
# ==========================================================
chrome_options = Options()
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1280,800")
chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

# ==========================================================
# 7. 원래의 수집 함수들 (온정찬, 런치투게더, 밥심, 런치타임)
# ==========================================================
def get_kakao_posts_image(driver, url):
    print(f" -> [온정찬] 카카오 게시물 이미지 수집 중")
    try:
        driver.get(url)
        time.sleep(4)
        posts = driver.find_elements(By.TAG_NAME, "div")
        for post in posts:
            try:
                text = post.text
                if today_date_str_space in text or today_date_str_nospace in text:
                    img = post.find_element(By.TAG_NAME, "img")
                    src = img.get_attribute("src")
                    if src and "k.kakaocdn.net/dn/" in src:
                        return src
            except:
                continue
        imgs = driver.find_elements(By.TAG_NAME, "img")
        for img in imgs:
            src = img.get_attribute("src")
            if src and "k.kakaocdn.net/dn/" in src:
                return src
        return None
    except Exception as e:
        print(f" -> [온정찬] 오류 : {e}")
        return None

def get_kakao_profile_image(driver, url, store_name):
    print(f" -> [{store_name}] 카카오 프로필 이미지 접근")
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
                kakao_imgs.append({"element": img, "src": src, "width": size["width"], "height": size["height"]})
            except:
                continue
        if not kakao_imgs:
            return None
        profile_candidates = [item for item in kakao_imgs if 0 < item["width"] <= 250 and item["height"] <= 250]
        if not profile_candidates:
            profile_candidates = [kakao_imgs[0]]
        profile = profile_candidates[0]
        try:
            driver.execute_script("arguments[0].click();", profile["element"])
        except:
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
                width, height = size["width"], size["height"]
                if width < 150 or height < 150:
                    continue
                modal_candidates.append({"src": src, "area": width * height})
            except:
                continue
        if modal_candidates:
            modal_candidates.sort(key=lambda x: x["area"], reverse=True)
            return modal_candidates[0]["src"]
        return profile["src"]
    except Exception as e:
        print(f" -> [{store_name}] 카카오 오류 : {e}")
        return None

def get_kakao_first_image(driver, url, store_name):
    print(f" -> [{store_name}] 카카오 최신 메뉴 이미지 수집")
    try:
        driver.get(url)
        time.sleep(3)
        imgs = driver.find_elements(By.TAG_NAME, "img")
        for img in imgs:
            src = img.get_attribute("src")
            if src and "k.kakaocdn.net/dn/" in src:
                return src
        return None
    except Exception as e:
        print(f" -> [{store_name}] 카카오 오류 : {e}")
        return None

def get_threads_menu(driver, url):
    print(f" -> [런치타임] 스레드 메뉴 수집 중 ({url})")
    try:
        driver.get(url)
        time.sleep(4)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        lines = body_text.split("\n")
        target_date1 = today_date_str_nospace
        target_date2 = today_date_str_space
        start_idx = -1
        for i, line in enumerate(lines):
            line_clean = line.strip()
            if target_date1 in line_clean or target_date2 in line_clean:
                start_idx = i
                break
        if start_idx == -1:
            start_idx = 0
        for i, line in enumerate(lines):
            if line.strip() in ["Reposts", "리포스트", "Media", "미디어"]:
                start_idx = i + 1
                break
        filtered_lines = []
        for line in lines[start_idx:]:
            line = line.strip().replace('\\', '')
            if not line:
                continue
            if "월" in line and "일" in line and target_date1 not in line and target_date2 not in line:
                break
            if line in ["스레드", "답글", "미디어", "리포스트", "팔로우", "언급", "로그인", "가입하기", "lunchtime_ypp", "Home", "Follow", "Mention", "Threads", "Replies", "Media", "Reposts", "Translate"]:
                continue
            if "팔로워" in line or "followers" in line or "시간 전" in line or "일 전" in line or line.endswith("h") or line.endswith("d") or line.isdigit():
                continue
            filtered_lines.append(line)
        last_tag_idx = -1
        for i, l in enumerate(filtered_lines):
            if l.startswith("#"):
                last_tag_idx = i
        if last_tag_idx != -1:
            filtered_lines = filtered_lines[:last_tag_idx + 1]
        if not filtered_lines:
            return "<div>오늘의 메뉴 내용을 찾지 못했습니다.</div>"
        formatted_text = "<br>".join(filtered_lines)
        return f'<div style="background-color:#f9f9f9; border:1px solid #ddd; padding:15px; border-radius:8px; text-align:left; font-size:14px; line-height:1.6; color:#333;">{formatted_text}</div>'
    except Exception as e:
        print(f" -> [런치타임] 스레드 오류 : {e}")
        return "<div>스레드 메뉴를 불러오지 못했습니다.</div>"

# ==========================================================
# 8. 데이터 수집 및 JSON 저장
# ==========================================================
scraped_data = []
print(f"\n{'='*60}\n자동 수집 시작 (총 5곳)\n{'='*60}")

for item in cafeteria_list:
    print(f"\n[{item['name']}] 정보 수집 중...")
    base_lat, base_lng = get_coords(item["address"])
    lat = base_lat + item["lat_offset"]
    lng = base_lng + item["lng_offset"]
    dist, walk_min = calculate_walking_info((base_lat, base_lng))
    
    html_content = ""
    if item["type"] == "ojeong":
        src = crop_ojeong_by_weekday(item["url"])
        html_content = f'<img src="{src}" style="display:block; margin:0 auto; max-width:100%; max-height:420px; border-radius:6px;">' if src else "<div>이미지 없음</div>"
    elif item["type"] == "kakao_posts":
        img_src = get_kakao_posts_image(driver, item["url"])
        html_content = f'<img src="{img_src}" style="display:block; margin:0 auto; max-width:100%; max-height:380px; border-radius:6px;">' if img_src else '<div>이미지 없음</div>'
    elif item["type"] == "kakao_profile":
        img_src = get_kakao_profile_image(driver, item["url"], item["name"])
        html_content = f'<img src="{img_src}" style="display:block; margin:0 auto; max-width:100%; max-height:380px; border-radius:6px;">' if img_src else '<div>이미지 없음</div>'
    elif item["type"] == "kakao_first":
        img_src = get_kakao_first_image(driver, item["url"], item["name"])
        html_content = f'<img src="{img_src}" style="display:block; margin:0 auto; max-width:100%; max-height:380px; border-radius:6px;">' if img_src else '<div>이미지 없음</div>'
    elif item["type"] == "threads":
        html_content = get_threads_menu(driver, item["url"])

    scraped_data.append({
        "name": item["name"],
        "lat": lat,
        "lng": lng,
        "dist": dist,
        "walk_min": walk_min,
        "html": html_content
    })
    time.sleep(1)

driver.quit()

output_data = {
    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "cafeterias": scraped_data
}

os.makedirs("data", exist_ok=True)
json_path = os.path.join("data", "menu.json")
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=4)

print("=" * 60)
print(f"🎉 데이터 수집 완료 및 {json_path} 생성 완료!")
print("=" * 60)
