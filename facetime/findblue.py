# Find the blue "Join New Call" button (RGB 59,113,246 = #3B71F6, the BLUE
# constant below) inside the FaceTime window.
# Usage: findblue.py <img> <scale> [wx wy ww wh]
import sys
from PIL import Image
path=sys.argv[1]; scale=float(sys.argv[2])
win=[float(a) for a in sys.argv[3:7]] if len(sys.argv)>=7 else None
im=Image.open(path).convert("RGB"); W,H=im.size; px=im.load()
x0,y0,x1,y1=0,0,W,H
if win:
    wx,wy,ww,wh=win
    x0=max(0,int(wx/scale)); x1=min(W,int((wx+ww)/scale))
    y0=max(0,int(wy/scale));  y1=min(H,int((wy+wh)/scale))
BLUE=(59,113,246)
def near(c,t,tol=40): return abs(c[0]-t[0])<=tol and abs(c[1]-t[1])<=tol and abs(c[2]-t[2])<=tol
pts=set()
for y in range(y0,y1):
    for x in range(x0,x1):
        if near(px[x,y],BLUE): pts.add((x,y))
# largest connected blob
best=None
while pts:
    s=pts.pop(); st=[s]; comp=[s]
    while st:
        x,y=st.pop()
        for dx in(-1,0,1):
            for dy in(-1,0,1):
                n=(x+dx,y+dy)
                if n in pts: pts.discard(n); st.append(n); comp.append(n)
    if len(comp)>=1500:
        xs=[p[0] for p in comp]; ys=[p[1] for p in comp]
        cx=sum(xs)/len(xs); cy=sum(ys)/len(ys)
        if best is None or len(comp)>best[2]: best=(cx,cy,len(comp))
print(f"{best[0]*scale:.0f},{best[1]*scale:.0f}" if best else "NONE")
