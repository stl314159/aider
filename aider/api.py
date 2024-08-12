import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aider import urls
from aider.coders import Coder
from aider.io import InputOutput
from aider.main import main as cli_main
from aider.scrape import Scraper

class CaptureIO(InputOutput):
    lines = []

    def tool_output(self, msg, log_only=False):
        if not log_only:
            self.lines.append(msg)
        super().tool_output(msg, log_only=log_only)

    def tool_error(self, msg):
        self.lines.append(msg)
        super().tool_error(msg)

    def get_captured_lines(self):
        lines = self.lines
        self.lines = []
        return lines

class State:
    keys = set()

    def init(self, key, val=None):
        if key in self.keys:
            return

        self.keys.add(key)
        setattr(self, key, val)
        return True

state = State()

def get_coder():
    coder = cli_main(return_coder=True)
    if not isinstance(coder, Coder):
        raise ValueError(coder)
    if not coder.repo:
        raise ValueError("API can currently only be used inside a git repo")

    io = CaptureIO(
        pretty=False,
        yes=True,
        dry_run=coder.io.dry_run,
        encoding=coder.io.encoding,
    )
    coder.commands.io = io

    for line in coder.get_announcements():
        coder.io.tool_output(line)

    return coder

coder = get_coder()

class AiderAPIHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.coder = coder
        super().__init__(*args, **kwargs)

        self.coder.yield_stream = True
        self.coder.stream = True
        self.coder.pretty = False

        self.initialize_state()

    def send_error_response(self, status_code, message):
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        error_response = json.dumps({
            "error": message
        }).encode('utf-8')
        self.wfile.write(error_response)

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode('utf-8'))

        handlers = {
            '/chat': self.handle_chat,
            '/get_model': self.handle_get_model,
            '/add_files': self.handle_add_files,
            '/add_web_page': self.handle_add_web_page,
            '/clear_chat_history': self.handle_clear_chat_history,
            '/run_command': self.handle_run_command,
            '/undo': self.handle_undo,
            '/get_chat_history': self.handle_get_chat_history,
            '/get_file_list': self.handle_get_file_list,
            '/get_file_content': self.handle_get_file_content,
            '/commit': self.handle_commit,
            '/lint': self.handle_lint,
            '/test': self.handle_test,
        }

        handler = handlers.get(self.path)
        if handler:
            response = handler(data)
            if isinstance(response, dict) and "error" in response:
                self.send_error_response(400, response["error"])
                return
        else:
            self.send_error_response(404, "Not Found")
            return
            return

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
        def json_serialize(obj):
            if isinstance(obj, set):
                return list(obj)
            raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

        self.wfile.write(json.dumps(response, default=json_serialize).encode('utf-8'))

    def handle_chat(self, data):
        message = data.get('message')
        if not message:
            return {"error": "No message provided"}

        self.coder.io.add_to_input_history(message)
        response_stream = self.coder.run_stream(message)
        response = "".join(response_stream)

        edits = []
        if self.coder.aider_edited_files:
            edit = {
                "fnames": self.coder.aider_edited_files,
                "commit_hash": self.coder.last_aider_commit_hash,
                "commit_message": self.coder.last_aider_commit_message,
            }
            if self.coder.last_aider_commit_hash:
                commits = f"{self.coder.last_aider_commit_hash}~1"
                diff = self.coder.repo.diff_commits(
                    self.coder.pretty,
                    commits,
                    self.coder.last_aider_commit_hash,
                )
                edit["diff"] = diff
            edits.append(edit)

        return {"response": response, "edits": edits}

    def handle_get_model(self):
        return {"model": self.coder.main_model.name}

    def handle_add_files(self, data):
        fnames = data.get('files', [])
        added = []
        for fname in fnames:
            if fname not in self.coder.get_inchat_relative_files():
                self.coder.add_rel_fname(fname)
                added.append(fname)
        return {"added": added}

    def handle_add_web_page(self, data):
        url = data.get('url')
        if not url:
            return {"error": "No URL provided"}

        if not state.scraper:
            state.scraper = Scraper(print_error=lambda x: None)

        content = state.scraper.scrape(url) or ""
        if content.strip():
            content = f"{url}\n\n" + content
            return {"content": content}
        else:
            return {"error": f"No web content found for {url}"}

    def handle_clear_chat_history(self):
        self.coder.done_messages = []
        self.coder.cur_messages = []
        return {"message": "Chat history cleared"}

    def handle_run_command(self, data):
        command = data.get('command')
        if not command:
            return {"error": "No command provided"}

        import subprocess
        try:
            result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            return {"output": result.stdout}
        except subprocess.CalledProcessError as e:
            return {"error": str(e), "output": e.output}

    def handle_undo(self, data):
        commit_hash = data.get('commit_hash')
        if not commit_hash:
            return {"error": "No commit hash provided"}

        if (state.last_aider_commit_hash != commit_hash
            or self.coder.last_aider_commit_hash != commit_hash):
            return {"error": f"Commit {commit_hash} is not the latest commit."}

        reply = self.coder.commands.cmd_undo(None)
        state.last_undone_commit_hash = commit_hash

        return {"message": "Undo successful", "reply": reply}

    def handle_get_chat_history(self):
        return {"history": self.coder.done_messages + self.coder.cur_messages}

    def handle_get_file_list(self):
        return {"files": self.coder.get_all_relative_files()}

    def handle_get_file_content(self, data):
        fname = data.get('file')
        if not fname:
            return {"error": "No file name provided"}

        content = self.coder.io.read_text(self.coder.abs_root_path(fname))
        if content is None:
            return {"error": f"Unable to read file {fname}"}
        return {"content": content}

    def handle_commit(self):
        if self.coder.repo and self.coder.auto_commits and not self.coder.dry_run:
            result = self.coder.commands.cmd_commit()
            return {"message": "Commit successful", "result": result}
        return {"error": "Unable to commit changes"}

    def handle_lint(self, data):
        fnames = data.get('files', [])
        result = self.coder.commands.cmd_lint(fnames=fnames)
        return {"result": result}

    def handle_test(self, data):
        test_cmd = data.get('test_cmd')
        if not test_cmd:
            return {"error": "No test command provided"}
        test_errors = self.coder.commands.cmd_test(test_cmd)
        return {"errors": list(test_errors) if isinstance(test_errors, set) else test_errors}

    def initialize_state(self):
        state.init("messages", [
            {"role": "info", "content": self.announce()},
            {"role": "assistant", "content": "How can I help you?"}
        ])
        state.init("last_aider_commit_hash", self.coder.last_aider_commit_hash)
        state.init("last_undone_commit_hash")
        state.init("recent_msgs_num", 0)
        state.init("web_content_num", 0)
        state.init("prompt")
        state.init("scraper")

        state.init("initial_inchat_files", self.coder.get_inchat_relative_files())

        if "input_history" not in state.keys:
            input_history = list(self.coder.io.get_input_history())
            seen = set()
            input_history = [x for x in input_history if not (x in seen or seen.add(x))]
            state.input_history = input_history
            state.keys.add("input_history")

    def announce(self):
        lines = self.coder.get_announcements()
        return "  \n".join(lines)

def run_api(port=8080):
    global coder
    coder = get_coder()

    server_address = ('', port)
    httpd = ThreadingHTTPServer(server_address, AiderAPIHandler)
    print(f"Starting API server on port {port}")
    httpd.serve_forever()

if __name__ == "__main__":
    run_api()
