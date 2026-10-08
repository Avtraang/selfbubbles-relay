# Find FaceTime admit (green check) button by color, restricted to the FaceTime window.
# Usage: findadmit.py <image> <scale> [wx wy ww wh] [topskip]
#   scale = screen_points per image_pixel (full.png -> 0.5)
#   wx,wy,ww,wh = FaceTime window rect in screen POINTS (optional but recommended)
#   topskip = points to skip below window top (skip title bar/traffic lights; default 55)
import sys
from PIL import Image

path = sys.argv[1]
scale = float(sys.argv[2])
win = [float(a) for a in sys.argv[3:7]] if len(sys.argv) >= 7 else None
topskip = float(sys.argv[7]) if len(sys.argv) >= 8 else 55.0

img = Image.open(path).convert("RGB")
W, H = img.size
px = img.load()

# pixel bounds to search (image pixels)
x0, y0, x1, y1 = 0, 0, W, H
if win:
    wx, wy, ww, wh = win
    x0 = max(0, int((wx) / scale))
    x1 = min(W, int((wx + ww) / scale))
    y0 = max(0, int((wy + topskip) / scale))          # skip title bar
    y1 = min(H, int((wy + wh - 40) / scale))           # skip bottom toolbar (~40pt)

GREEN = (52, 199, 89); RED = (255, 59, 48)
def near(c, t, tol=48):
    return abs(c[0]-t[0])<=tol and abs(c[1]-t[1])<=tol and abs(c[2]-t[2])<=tol

greens, reds = set(), set()
for y in range(y0, y1):
    for x in range(x0, x1):
        c = px[x, y]
        if near(c, GREEN): greens.add((x, y))
        elif near(c, RED): reds.add((x, y))

def clusters(pts):
    pts = set(pts); out = []
    while pts:
        s = pts.pop(); st=[s]; comp=[s]
        while st:
            x,y = st.pop()
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    n=(x+dx,y+dy)
                    if n in pts: pts.discard(n); st.append(n); comp.append(n)
        xs=[p[0] for p in comp]; ys=[p[1] for p in comp]
        out.append((sum(xs)/len(xs), sum(ys)/len(ys), len(comp)))
    return [c for c in out if c[2] >= 25]

gc = clusters(greens); rc = clusters(reds)
for g in gc: print(f"# green px({g[0]:.0f},{g[1]:.0f}) n={g[2]}", file=sys.stderr)
for r in rc: print(f"# red   px({r[0]:.0f},{r[1]:.0f}) n={r[2]}", file=sys.stderr)

best=None
for g in gc:
    for r in rc:
        if 5 < (g[0]-r[0]) < 130 and abs(g[1]-r[1]) < 40: best=g
if best is None and gc:
    best=max(gc, key=lambda g:g[2])
print(f"{best[0]*scale:.0f},{best[1]*scale:.0f}" if best else "NONE")
