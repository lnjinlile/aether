#!/usr/bin/env python3
"""Pure Python Trojan proxy → SOCKS5 local server for Binance mainnet access."""
import socket, ssl, struct, threading, hashlib

SERVER = "0cfbd3c0-fdc0-4f26-82fb-5c8f8b6ce82b.bnsepserv.com"
PORT = 32039
PASSWORD = "3cc27cef-08ad-456f-8c60-d88f6590cb25"
SNI = "pull-flv-t13-admin.douyincdn.com"
SOCKS_PORT = 7890

def trojan_connect(host, port):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((SERVER, PORT), timeout=15)
    tls = ctx.wrap_socket(raw, server_hostname=SNI)

    pwd_hash = hashlib.sha224(PASSWORD.encode()).hexdigest()
    header = (pwd_hash + "\r\n").encode()
    addr_bytes = host.encode()
    header += struct.pack('!BBB', 0x01, 0x03, len(addr_bytes)) + addr_bytes + struct.pack('!H', port) + b"\r\n"
    tls.sendall(header)
    return tls

def handle_client(client_sock):
    try:
        data = client_sock.recv(262)
        if not data or data[0] != 0x05: client_sock.close(); return
        client_sock.sendall(b'\x05\x00')
        data = client_sock.recv(262)
        if len(data) < 10 or data[1] != 0x01:
            client_sock.sendall(b'\x05\x07\x00\x01' + b'\x00'*6); client_sock.close(); return
        atyp = data[3]
        if atyp == 0x03:
            alen = data[4]; host = data[5:5+alen].decode()
            port = struct.unpack('!H', data[5+alen:7+alen])[0]
        elif atyp == 0x01:
            host = socket.inet_ntoa(data[4:8])
            port = struct.unpack('!H', data[8:10])[0]
        else:
            client_sock.sendall(b'\x05\x08\x00\x01' + b'\x00'*6); client_sock.close(); return
        remote = trojan_connect(host, port)
        client_sock.sendall(b'\x05\x00\x00\x01' + b'\x00'*6)
        def relay(src, dst):
            try:
                while True:
                    d = src.recv(8192)
                    if not d: break
                    dst.sendall(d)
            except: pass
        t1 = threading.Thread(target=relay, args=(client_sock, remote), daemon=True)
        t2 = threading.Thread(target=relay, args=(remote, client_sock), daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=300); t2.join(timeout=300)
    except:
        try: client_sock.close()
        except: pass

if __name__ == "__main__":
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', SOCKS_PORT))
    server.listen(10)
    print(f"🟢 SOCKS5 proxy on 127.0.0.1:{SOCKS_PORT} → 🇨🇦 Canada")
    while True:
        try:
            c, _ = server.accept()
            threading.Thread(target=handle_client, args=(c,), daemon=True).start()
        except KeyboardInterrupt: break
        except: pass
