import ctypes, ctypes.util, time, sys
cg = ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
# jiggle around a center point (args: cx cy [seconds])
cx, cy = float(sys.argv[1]), float(sys.argv[2])
dur = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
t0 = time.time()
i = 0
while time.time() - t0 < dur:
    dx = (i % 5) * 12 - 24
    dy = (i % 3) * 12 - 12
    e = cg.CGEventCreateMouseEvent(None, 5, CGPoint(cx+dx, cy+dy), 0)  # 5 = mouseMoved
    cg.CGEventPost(0, e)  # 0 = HID tap
    i += 1
    time.sleep(0.08)
