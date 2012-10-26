import concurrent.futures
import os
import platform
import socket
import sys

esc = 'strict' if len(platform.win32_ver()[0]) > 0 else 'surrogateescape'
enc = sys.getfilesystemencoding()


def unicode_to_wsgi(u):
    '''Convert an environment variable to a WSGI "bytes-as-unicode" string
    '''
    return u.encode(enc, esc).decode('iso-8859-1')


def wsgi_to_bytes(s):
    '''Convert an WSGI "bytes-as-unicode" string
    '''
    return s.encode('iso-8859-1')


def build_headers(**headers):
    return "\r\n".join(['%s: %s' % (name, headers[name]) for name in headers])


def parse_headers(data):
    string_data = data.decode('ascii')
    protocol_line, header_lines = string_data.split('\r\n', 1)
    method, tail = protocol_line.split(' ', 1)
    query, protocol = tail.rsplit(' ', 1)
    headers = [line.split(': ') for line in header_lines.split('\r\n')]
    return method, query, protocol, dict(headers)


def server(host, port):
    '''Run server
    '''
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket.bind((host, port))
    serversocket.listen(1024)
    while True:
        conn, addr = serversocket.accept()
        yield conn, addr


def handle_connection(conn, addr, application):
    # Parse the request, prepare request object
    data = b''
    print('Connection %s:%s' % addr)
    while True:
        print('Read chunk')
        chunk = conn.recv(1024)
        print(chunk)
        if not chunk:
            break
        data += chunk
    try:
        header_data, body_data = data.split(b'\r\n\r\n')
        method, query, protocol, headers = parse_headers(header_data)
        #if method in METHODS_WITH_BODY:
        #    if 'Content-Length' not in headers:
        #        raise Exception('No Content-Length header')
        #    data_length = int(headers['Content-Length'])
        #else:
        #    body = b''
    except Exception:
        import sys
        import traceback
        traceback.print_exc(file=sys.stdout)
    # Prepare environment
    environ = {k: unicode_to_wsgi(v) for k,v in os.environ.items()}
    environ['wsgi.input'] = body_data
    environ['wsgi.errors'] = sys.stderr
    environ['wsgi.version'] = (1, 0)
    environ['wsgi.multithread'] = False
    environ['wsgi.multiprocess'] = True
    environ['wsgi.run_once'] = True

    if environ.get('HTTPS', 'off') in ('on', '1'):
        environ['wsgi.url_scheme'] = 'https'
    else:
        environ['wsgi.url_scheme'] = 'http'

    headers_set = None

    def write(data):
        conn.send()

    def start_response(status, response_headers, exc_info=None):
        '''Function that starts the response sending headers
        '''
        # Playing with exceptions
        if exc_info:
            try:
                if headers_set:
                    # Re-raise original exception if headers sent
                    raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None     # avoid dangling circular ref
        elif headers_set:
            raise AssertionError("Headers already set!")
        # Prepare headers and send it
        headers_set = [b'HTTP/1.1 ', wsgi_to_bytes(status),
                       b'\r\nStatus: ', wsgi_to_bytes(status), b'\r\n']
        headers_set.extend([wsgi_to_bytes('%s: %s\r\n' % header)
                            for header in response_headers])
        headers_set.append(b'\r\n')
        conn.send(b''.join(headers_set))
        return write
    # Execute application and send the response
    result = application(environ, start_response)
    try:
        for data in result:
            if data:
                write(data)
    finally:
        if hasattr(result, 'close'):
            result.close()
    conn.close()


def my_handler(request, start_response):
    '''Simple test handler
    '''
    data = "Hello, World!\n"
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("Content-Length", str(len(data)))])
    return iter([data])


def run_server(host, port, application):
    #with concurrent.futures.ProcessPoolExecutor() as executor:
    for conn, addr in server(host, port):
        #    executor.submit(
        handle_connection(conn, addr, application)


if __name__ == '__main__':
    run_server('127.0.0.1', 8080, my_handler)
