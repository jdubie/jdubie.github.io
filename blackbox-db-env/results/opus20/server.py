import socket, struct, threading, os, sys
MAGIC=0x5032c838
GREET=b'*\x12\x00\x0cObsidian 8.7'+struct.pack(">I",MAGIC)
def S(x): return struct.pack(">H",len(x))+x
def err(code,msg):
    b=b'H'+S(code)+b'G'+S(b'MURE')+b'U'+S(msg.encode())+b'v'+S(b'MURE')+b'\0'
    return b','+enc(len(b))+b
def enc(n):
    o=b''
    while True:
        x=n&127; n>>=7
        if n: o+=bytes([x|128])
        else: return o+bytes([x])
def recvn(c,n):
    d=b''
    while len(d)<n:
        x=c.recv(n-len(d))
        if not x: raise EOFError
        d+=x
    return d
def handle(c):
    try:
        c.sendall(GREET)
        L=0; sh=0
        while True:
            x=recvn(c,1)[0]; L|=(x&127)<<sh; sh+=7
            if not x&128 or sh>35: break
        if L<4 or L>10000:
            c.sendall(err(b'0LP01','invalid length of startup packet')); return
        body=recvn(c,L)
        v=struct.unpack(">I",body[:4])[0]
        if v!=MAGIC:
            c.sendall(err(b'2F000','unsupported frontend protocol %d: server supports %d'%(v,MAGIC))); return
        TERM=err(b'0LP01','invalid startup packet layout: expected terminator as last byte')
        pos=4; params=[]
        while True:
            if pos+2>L: c.sendall(TERM); return
            kl=struct.unpack(">H",body[pos:pos+2])[0]; pos+=2
            if kl==0:
                if pos!=L: c.sendall(TERM); return
                break
            if pos+kl+2>L: c.sendall(TERM); return
            k=body[pos:pos+kl]; pos+=kl
            vl=struct.unpack(">H",body[pos:pos+2])[0]; pos+=2
            if pos+vl>L: c.sendall(TERM); return
            v=body[pos:pos+vl]; pos+=vl
            params.append((k,v))
        if params:
            c.sendall(err(b'54704','unrecognized configuration parameter "%s"'%params[0][0].decode('utf-8','replace'))); return
        c.sendall(err(b'72000','no Obsidian user name specified in startup packet'))
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
def main():
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind(("0.0.0.0",7000)); s.listen(128)
    while True:
        c,_=s.accept()
        threading.Thread(target=handle,args=(c,),daemon=True).start()
main()
