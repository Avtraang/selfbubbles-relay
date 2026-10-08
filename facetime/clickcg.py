# Real HID mouse click(s) via CoreGraphics. Works from bash (no AX needed).
#   clickcg.py x y [count] [hold_ms]
import ctypes, ctypes.util, sys, time
cg = ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
class P(ctypes.Structure): _fields_=[("x",ctypes.c_double),("y",ctypes.c_double)]
cg.CGEventCreateMouseEvent.restype=ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint32,P,ctypes.c_uint32]
cg.CGEventPost.argtypes=[ctypes.c_uint32,ctypes.c_void_p]
MOVE,DOWN,UP=5,1,2
x,y=float(sys.argv[1]),float(sys.argv[2])
cnt=int(sys.argv[3]) if len(sys.argv)>3 else 1
hold=(float(sys.argv[4])/1000.0) if len(sys.argv)>4 else 0.06
def post(t): 
    e=cg.CGEventCreateMouseEvent(None,t,P(x,y),0); cg.CGEventPost(0,e)
post(MOVE); time.sleep(0.05)
for _ in range(cnt):
    post(DOWN); time.sleep(hold); post(UP); time.sleep(0.12)
print(f"clicked {x:.0f},{y:.0f} x{cnt}")
