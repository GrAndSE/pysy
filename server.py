#import concurrent.futures
import functools
import os
import platform
import select
import socket
import sys

METHODS_WITH_BODY = frozenset(['POST', 'PUT', 'PATCH', ])

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
    path, protocol = tail.rsplit(' ', 1)
    headers = [line.split(': ') for line in header_lines.split('\r\n')]
    return method, path, protocol, dict(headers)


def server(host, port, application):
    '''Run server
    '''
    # Prepare base environment
    environ = {k: unicode_to_wsgi(v) for k,v in os.environ.items()}
    environ['wsgi.errors'] = sys.stderr
    environ['wsgi.version'] = (1, 0)
    environ['wsgi.multithread'] = False
    environ['wsgi.multiprocess'] = True
    environ['wsgi.run_once'] = True
    environ['SERVER_NAME'] = host
    environ['SERVER_PORT'] = port
    if environ.get('HTTPS', 'off') in ('on', '1'):
        environ['wsgi.url_scheme'] = 'https'
    else:
        environ['wsgi.url_scheme'] = 'http'
    # Create socket
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serversocket.bind((host, port))
    serversocket.listen(50)
    serversocket.setblocking(0)

    epoll = select.epoll()
    epoll.register(serversocket.fileno(), select.EPOLLIN)
    try:
        connections = {}
        requests = {}
        envs = {}
        responses = {}
        ct_length = {}
        while True:
            events = epoll.poll(1)
            for fileno, event in events:
                if fileno == serversocket.fileno():
                    conn, addr = serversocket.accept()
                    conn.setblocking(0)
                    conn_fileno = conn.fileno()
                    epoll.register(conn_fileno, select.EPOLLIN)
                    connections[conn_fileno] = conn
                    requests[conn_fileno] = b''
                elif event & select.EPOLLIN:
                    data = connections[fileno].recv(1024)
                    if not data:
                        epoll.modify(fileno, select.EPOLLET)
                        connections[fileno].shutdown(socket.SHUT_RDWR)
                        continue
                    if fileno in ct_length:
                        envs[fileno]['wsgi.input'] += data
                        if ct_length[fileno] <= len(envs[fileno]['wsgi.input']):
                            epoll.modify(fileno, select.EPOLLOUT)
                            responses[fileno] = handle_request(envs[fileno],
                                                               application)
                        continue
                    requests[fileno] += data
                    if b'\r\n\r\n' in requests[fileno]:
                        #print('-'*40, '\n', requests[fileno])
                        header_data, body_data = ((requests[fileno][:-4], b'')
                                        if requests[fileno].endswith(b'\r\n\r\n')
                                        else requests[fileno].split(b'\r\n\r\n', 1))
                        method, query, protocol, headers = parse_headers(header_data)
                        path, qs = (query.split('?')
                                    if '?' in query and not query.endswith('?')
                                    else (query, ''))
                        __, script_name = path.rsplit('/', 1)
                        envs[fileno] = {
                            'REQUEST_METHOD': method,
                            'SCRIPT_NAME': script_name,
                            'PATH_INFO': path,
                            'QUERY_STRING': qs,
                            'HTTP_HEADERS': headers,
                            'CONTENT_LENGTH': int(headers.get('Content-Length', 0)),
                            'CONTENT_TYPE': headers.get('Content-Type', ''),
                            'SERVER_PROTOCOL': protocol,
                            'wsgi.input': body_data
                        }
                        envs[fileno].update(environ)
                        del requests[fileno]
                        if method in METHODS_WITH_BODY:
                            if not 'Content-Length' in headers:
                                # Drop request without a Content-Length specified
                                epoll.modify(fileno, 0)
                                connections[fileno].shutdown(socket.SHUT_RDWR)
                            else:
                                ct_length[fileno] = int()
                                if ct_length[fileno] <= len(body_data):
                                    epoll.modify(fileno, select.EPOLLOUT)
                                    responses[fileno] = handle_request(envs[fileno],
                                                                       application)
                        else:
                            epoll.modify(fileno, select.EPOLLOUT)
                            responses[fileno] = handle_request(envs[fileno],
                                                               application)

                elif event & select.EPOLLOUT:
                    if fileno in responses:
                        bytes_written = connections[fileno].send(responses[fileno])
                        responses[fileno] = responses[fileno][bytes_written:]
                        if len(responses[fileno]) == 0:
                            epoll.modify(fileno, 0)
                            connections[fileno].shutdown(socket.SHUT_RDWR)
                elif event & select.EPOLLHUP:
                    epoll.unregister(fileno)
                    connections[fileno].close()
                    del connections[fileno]
    finally:
        epoll.unregister(serversocket.fileno())
        epoll.close()
        serversocket.close()


def write_to_response(response, data):
    '''Write some data into response
    '''
    response += wsgi_to_bytes(data)


def start_response_base(write, headers_set, status, response_headers,
                        exc_info=None):
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
    headers_set = (['HTTP/1.1 ', status, '\r\nStatus: ', status, '\r\n'] +
                   ['%s: %s\r\n' % header for header in response_headers] +
                   ['\r\n'])
    write(''.join(headers_set))
    return write


def handle_request(environ, application):
    headers_set = None
    response = b''

    write = functools.partial(write_to_response, response)
    start_response = functools.partial(start_response_base, write, headers_set)

    # Execute application and send the response
    result = application(environ, start_response)
    try:
        for data in result:
            if data:
                write(data)
    finally:
        if hasattr(result, 'close'):
            result.close()
    return response


def my_handler(request, start_response):
    '''Simple test handler
    '''
    data = "Hello, World!\n"
    start_response("200 OK", [("Content-Type", "text/plain"),
                              ("Content-Length", str(len(data)))])
    return iter([data])


def run_server(host, port, application):
    server(host, port, application)


if __name__ == '__main__':
    run_server('127.0.0.1', 8080, my_handler)
