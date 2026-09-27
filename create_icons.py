import os
import subprocess
from PIL import Image, ImageDraw, ImageFilter

def make_menubar_icons():
    """상단 메뉴바용 깔끔한 흑백 템플릿 아이콘 생성 (SF Symbols 스타일)"""
    scale = 8
    w, h = 44 * scale, 44 * scale
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    cx = 22 * scale
    capsule_w = 11 * scale
    capsule_h = 17 * scale
    x0 = cx - capsule_w // 2
    x1 = cx + capsule_w // 2
    y0 = 6 * scale
    y1 = y0 + capsule_h
    r = capsule_w // 2
    # 마이크 캡슐
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(0, 0, 0, 255))
    
    # 크래들 호(Arc)
    cradle_pad = 4.2 * scale
    arc_box = [x0 - cradle_pad, y0 + 5.5 * scale, x1 + cradle_pad, y1 + 5.2 * scale]
    stroke_w = int(2.4 * scale)
    draw.arc(arc_box, start=0, end=180, fill=(0, 0, 0, 255), width=stroke_w)
    
    # 기둥(Stem)
    stem_top = arc_box[3] - stroke_w // 2
    stem_bottom = stem_top + int(6.5 * scale)
    draw.line([cx, stem_top, cx, stem_bottom], fill=(0, 0, 0, 255), width=stroke_w)
    
    # 받침(Base)
    base_w = 13 * scale
    draw.line([cx - base_w // 2, stem_bottom, cx + base_w // 2, stem_bottom], fill=(0, 0, 0, 255), width=stroke_w)
    
    # 리사이즈
    icon_44 = img.resize((44, 44), Image.Resampling.LANCZOS)
    icon_22 = img.resize((22, 22), Image.Resampling.LANCZOS)
    
    os.makedirs("assets", exist_ok=True)
    icon_44.save("assets/iconTemplate@2x.png", "PNG")
    icon_22.save("assets/iconTemplate.png", "PNG")
    icon_44.save("assets/icon.png", "PNG")
    print("메뉴바 템플릿 아이콘 생성 완료")

def make_app_icon():
    """Spotlight 및 Launchpad용 고해상도 앱 아이콘 (1024x1024) 및 .icns 생성"""
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # macOS 표준 둥근 사각형 모서리
    # 배경: 세련된 다크/인디고 그라데이션
    for y in range(size):
        ratio = y / size
        # 상단 짙은 네이비/퍼플 (28, 30, 48) -> 하단 딥 슬레이트 (15, 17, 26)
        r = int(32 * (1 - ratio) + 16 * ratio)
        g = int(36 * (1 - ratio) + 18 * ratio)
        b = int(58 * (1 - ratio) + 32 * ratio)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
        
    # 부드러운 앱 아이콘 둥근 사각형 마스크 (Squircle 유사 220px 라운드)
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    margin = 40
    mask_draw.rounded_rectangle([margin, margin, size - margin, size - margin], radius=220, fill=255)
    
    # 앱 배경에 마스크 적용
    app_bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    app_bg.paste(img, (0, 0), mask=mask)
    
    # 중앙 세련된 마이크 + 네온 사운드 그래픽
    fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fg_draw = ImageDraw.Draw(fg)
    
    cx, cy = 512, 470
    
    # 마이크 캡슐
    cap_w, cap_h = 160, 260
    fg_draw.rounded_rectangle(
        [cx - cap_w // 2, cy - cap_h // 2, cx + cap_w // 2, cy + cap_h // 2],
        radius=cap_w // 2,
        fill=(255, 255, 255, 245)
    )
    
    # 크래들
    cradle_pad = 70
    arc_box = [cx - cap_w // 2 - cradle_pad, cy - cap_h // 2 + 80, cx + cap_w // 2 + cradle_pad, cy + cap_h // 2 + 80]
    fg_draw.arc(arc_box, start=0, end=180, fill=(255, 255, 255, 245), width=36)
    
    # 기둥 & 받침
    stem_bottom = cy + cap_h // 2 + 180
    fg_draw.line([cx, cy + cap_h // 2 + 80, cx, stem_bottom], fill=(255, 255, 255, 245), width=36)
    fg_draw.line([cx - 100, stem_bottom, cx + 100, stem_bottom], fill=(255, 255, 255, 245), width=36)
    
    # 오디오 음파 액센트 (레드/코랄 녹음 인디케이터 링)
    fg_draw.ellipse([cx - 30, cy + 300, cx + 30, cy + 360], fill=(255, 75, 75, 255))
    
    # 합성
    final_icon = Image.alpha_composite(app_bg, fg)
    
    # iconutil을 위한 icon.iconset 폴더 빌드
    iconset_dir = "assets/RecordBrief.iconset"
    os.makedirs(iconset_dir, exist_ok=True)
    
    icon_sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    
    for s, name in icon_sizes:
        resized = final_icon.resize((s, s), Image.Resampling.LANCZOS)
        resized.save(os.path.join(iconset_dir, name), "PNG")
        
    final_icon.save("assets/app_icon_1024.png", "PNG")
    
    # iconutil로 .icns 생성
    icns_path = "assets/RecordBrief.icns"
    subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icns_path], check=True)
    print(f"생성 완료: {icns_path}")

if __name__ == "__main__":
    make_menubar_icons()
    make_app_icon()
