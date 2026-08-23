import http.server, socketserver
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", 5174), http.server.SimpleHTTPRequestHandler)
print("serving 5174", flush=True)
httpd.serve_forever()
