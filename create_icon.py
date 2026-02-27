#!/usr/bin/env python3
"""
Create a favicon.ico for ChatHub.

This script generates a simple icon with "CH" text.
Run: python create_icon.py
"""

from PIL import Image, ImageDraw, ImageFont
import os
from pathlib import Path


def create_favicon():
    """Create a favicon.ico with multiple sizes."""
    project_dir = Path(__file__).resolve().parent
    static_dir = project_dir / 'static'
    static_dir.mkdir(exist_ok=True)

    # Icon sizes for .ico file
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        # Create image with transparent background
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Draw rounded rectangle background (green)
        margin = max(1, size // 16)
        radius = size // 4
        bbox = [margin, margin, size - margin, size - margin]

        # Draw background circle
        draw.ellipse(bbox, fill=(76, 175, 80, 255))

        # Add "CH" text
        text = "CH"
        try:
            # Calculate font size based on icon size
            font_size = int(size * 0.4)
            font = ImageFont.truetype("arial.ttf", font_size)
        except:
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", int(size * 0.4))
            except:
                font = ImageFont.load_default()

        # Get text bounding box
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        # Center text
        x = (size - text_width) // 2
        y = (size - text_height) // 2 - (text_bbox[1] if text_bbox[1] else 0)

        # Draw text
        draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

        images.append(img)

    # Save as .ico
    ico_path = static_dir / 'favicon.ico'
    images[0].save(
        ico_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print(f"Created: {ico_path}")

    # Also save as PNG for web use
    png_path = static_dir / 'favicon.png'
    images[-1].save(png_path, format='PNG')
    print(f"Created: {png_path}")


if __name__ == '__main__':
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Installing Pillow...")
        import subprocess
        import sys
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'pillow'])
        from PIL import Image, ImageDraw, ImageFont

    create_favicon()
    print("\nFavicon created successfully!")
