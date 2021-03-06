from logging import getLogger, FileHandler
from paramiko import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    RSAKey,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED as OPEN_FAILED,
    OPEN_SUCCEEDED,
    ServerInterface,
    SSHClient,
    Transport,
    WarningPolicy,
)
from pathlib import Path
from socket import AF_INET, socket, SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR
from string import printable
from threading import Event, Thread
from time import sleep

from eNMS.database import Session
from eNMS.database.functions import fetch


class Client(SSHClient):
    def __init__(self, hostname, username, password):
        super().__init__()
        self.load_system_host_keys()
        self.set_missing_host_key_policy(WarningPolicy)
        self.connect(
            hostname=hostname, username=username, password=password,
        )
        self.shell = self.invoke_shell()


class Server(ServerInterface):
    def __init__(self, port, uuid):
        self.event = Event()
        self.username = uuid
        sock = socket(AF_INET, SOCK_STREAM)
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        sock.bind(("", port))
        sock.listen(100)
        sock.settimeout(30)
        self.transport = Transport(sock.accept()[0])
        try:
            host_key = RSAKey.from_private_key_file("rsa.key")
        except FileNotFoundError:
            host_key = RSAKey.generate(2048)
            host_key.write_private_key_file("rsa.key")
        self.transport.add_server_key(host_key)
        self.transport.start_server(server=self)
        self.channel = self.transport.accept(10)

    def check_channel_request(self, kind, *_):
        return OPEN_SUCCEEDED if kind == "session" else OPEN_FAILED

    def check_auth_none(self, username):
        return AUTH_SUCCESSFUL if username == self.username else AUTH_FAILED

    def check_channel_shell_request(self, *_):
        self.event.set()
        return True

    def check_channel_pty_request(self, *_):
        return True


class SshConnection:
    def __init__(self, hostname, username, password, session_id, uuid, port):
        self.client = Client(hostname, username, password)
        self.server = Server(port, uuid)
        path = Path.cwd() / "logs" / "ssh_sessions"
        path.mkdir(parents=True, exist_ok=True)
        self.logger = getLogger(hostname)
        if not self.logger.handlers:
            filehandler = FileHandler(filename=path / f"{hostname}.log")
            self.logger.addHandler(filehandler)
        Thread(target=self.receive_data, args=(session_id,)).start()
        Thread(target=self.send_data).start()

    def receive_data(self, session_id):
        log, session = "", fetch("session", id=session_id)
        while not self.client.shell.closed:
            response = self.client.shell.recv(1024)
            if not response:
                continue
            self.server.channel.send(response)
            log += "".join(c for c in str(response, "utf-8") if c in printable)
            if "\n" not in log:
                continue
            else:
                parsed_log = "\n".join(l for l in log.splitlines() if l)
                self.logger.info(parsed_log)
                session.content += parsed_log
                log = ""
            sleep(0.1)
        Session.commit()

    def send_data(self):
        while not self.client.shell.closed:
            data = self.server.channel.recv(512)
            if not data:
                break
            self.client.shell.send(data)
            sleep(0.1)
        self.client.shell.close()
        self.server.transport.close()
