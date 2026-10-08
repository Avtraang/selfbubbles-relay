# Keep the cursor parked at x,y (tiny in-place moves) for N seconds, so hover UI stays visible.
import ctypes, ctypes.util, sys, time
cg=ctypes.CDLL(ctypes.util.find_library('CoreGraphics'))
class P(ctypes.Structure): _fields_=[("x",ctypes.c_double),("y",ctypes.c_double)]
cg.CGEventCreateMouseEvent.restype=ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint32,P,ctypes.c_uint32]
cg.CGEventPost.argtypes=[ctypes.c_uint32,ctypes.c_void_p]
x,y=float(sys.argv[1]),float(sys.argv[2]); dur=float(sys.argv[3]) if len(sys.argv)>3 else 3
t0=time.time(); i=0
while time.time()-t0<dur:
    e=cg.CGEventCreateMouseEvent(None,5,P(x+(i%2),y+((i+1)%2)),0); cg.CGEventPost(0,e)
    i+=1; time.sleep(0.1)
