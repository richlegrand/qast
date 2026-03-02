import time
from qast import Qast

print("Connecting...")
q = Qast(device="roku", cookies_from_browser="chrome")

q.add("https://www.youtube.com/watch?v=PwylW_sUfQY", duration=15)
q.add("https://www.youtube.com/watch?v=aOAzJ37Nxfw", duration=15)
q.add("screen", duration=15)
q.add("window:sublime", duration=15)
q.add("browser:pixycam.com", duration=15)
q.add("webcam", duration=15)

q.play(repeat=True, verbose=True)                              # starts casting (non-blocking)
q.add("out.mp4", duration=15)         # add 
while True:            
    s = q.status()                                                       
    print(s)
    time.sleep(5)

'''
Other methods:
q.remove(2)                           # remove item by index
q.skip()                              # skip to next item
q.stop()                              # stop and disconnect
'''