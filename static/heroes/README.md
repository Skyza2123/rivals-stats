# Hero Images Guide

## How to Add Hero Pictures

The Top 5 Banned/Protected Heroes sections automatically load hero images from this directory.

### File Naming Convention

Hero image files should be named using the following convention:
- Convert the hero name to lowercase
- Replace spaces with underscores
- Replace `&` with `and`
- Remove periods (`.`)
- Replace hyphens (`-`) with underscores
- Use `.png` file extension

**Examples:**
- `Adam Warlock` → `adam_warlock.png`
- `Black Panther` → `black_panther.png`
- `Cloak & Dagger` → `cloak_and_dagger.png`
- `Dr. Strange` → `dr_strange.png`
- `Spider-Man` → `spider_man.png`
- `Peni Parker` → `peni_parker.png`

### Image Specifications

- **Format:** PNG (recommended), JPG, or WebP
- **Size:** Recommended 150x150px or larger
- **Aspect Ratio:** Square (1:1)
- **Style:** Hero portraits/character artwork

### Fallback Behavior

If a hero image is not found in this directory:
1. The app will display a placeholder image with the hero name
2. No errors will be logged
3. The layout will not break

### File Structure Example

```
static/
├── heroes/
│   ├── adam_warlock.png
│   ├── black_panther.png
│   ├── blade.png
│   ├── captain_america.png
│   ├── cloak_and_dagger.png
│   ├── dr_strange.png
│   ├── spider_man.png
│   └── ... (more hero images)
├── uploads/
└── style.css
```

### Storage Location

Place all hero image files directly in: `static/heroes/`

No subdirectories are needed.
