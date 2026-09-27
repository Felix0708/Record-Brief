from PIL import Image, ImageDraw

def create_smooth_mic_icon():
    scale = 8  # 8x supersampling for perfect anti-aliasing
    w, h = 44 * scale, 44 * scale
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Coordinates scaled up
    # Mic capsule: center at 22*scale = 176
    # Width = 11*scale, height = 18*scale, radius = 5.5*scale
    cx = 22 * scale
    capsule_w = 12 * scale
    capsule_h = 18 * scale
    x0 = cx - capsule_w // 2
    x1 = cx + capsule_w // 2
    y0 = 6 * scale
    y1 = y0 + capsule_h
    r = capsule_w // 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(0, 0, 0, 255))
    
    # Cradle arc
    cradle_pad = 4.5 * scale
    arc_box = [x0 - cradle_pad, y0 + 6 * scale, x1 + cradle_pad, y1 + 5.5 * scale]
    stroke_w = int(2.6 * scale)
    draw.arc(arc_box, start=0, end=180, fill=(0, 0, 0, 255), width=stroke_w)
    
    # Stem
    stem_top = arc_box[3] - stroke_w // 2
    stem_bottom = stem_top + int(6.5 * scale)
    draw.line([cx, stem_top, cx, stem_bottom], fill=(0, 0, 0, 255), width=stroke_w)
    
    # Base
    base_w = 14 * scale
    draw.line([cx - base_w // 2, stem_bottom, cx + base_w // 2, stem_bottom], fill=(0, 0, 0, 255), width=stroke_w)
    
    # Downsample
    icon_44 = img.resize((44, 44), Image.Resampling.LANCZOS)
    icon_22 = img.resize((22, 22), Image.Resampling.LANCZOS)
    
    icon_44.save("assets/iconTemplate@2x.png", "PNG")
    icon_22.save("assets/iconTemplate.png", "PNG")
    icon_44.save("assets/icon.png", "PNG")
    print("Generated smooth monochrome template icons in assets/")

if __name__ == "__main__":
    create_smooth_mic_icon()
