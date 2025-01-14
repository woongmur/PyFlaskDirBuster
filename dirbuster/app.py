import os
import aiohttp
import asyncio
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, request, render_template, send_file, jsonify
import requests
from markupsafe import Markup

# Flask 애플리케이션 초기화
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

# 에러 로그를 관리하는 클래스 정의
class ErrorLogger:
    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.error_log_file = None
        self.error_logs = {}

        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

    def create_new_error_log_file(self):
        current_time = datetime.now()
        log_filename = current_time.strftime("%Y%m%d%H%M%S") + '_error_log.txt'
        log_file_path = os.path.join(self.log_dir, log_filename)
        self.error_log_file = open(log_file_path, 'a')
        print(f"Created new error log file: {log_file_path}")

    def close_error_log_file(self):
        if self.error_log_file:
            self.error_log_file.close()
            print("Closed error log file.")

    async def log_error(self, test_url, status, error_message):
        if self.error_log_file is None or self.error_log_file.closed:
            self.create_new_error_log_file()

        log_message = f"{error_message}\n({test_url})\n"
        if status not in self.error_logs:
            self.error_logs[status] = []
        self.error_logs[status].append(log_message)
        print(f"Error logged: {log_message}")

    def write_error_logs(self):
        if self.error_logs:
            for status, messages in self.error_logs.items():
                self.error_log_file.write(f"\n{status}\n")
                self.error_log_file.writelines(messages)

    def reset_logs(self):
        self.error_logs = {}
        self.close_error_log_file()
        self.error_log_file = None

# 디렉토리 트리 구조를 관리하는 클래스 정의
class DirectoryTree:
    def __init__(self):
        self.tree = {}

    def add_path(self, path, is_file=False):
        parts = path.strip('/').split('/')
        current = self.tree
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        last_part = parts[-1]
        if is_file:
            current[last_part] = 'file'
        else:
            if last_part not in current:
                current[last_part] = {}

    def display(self, current=None, prefix=''):
        if current is None:
            current = self.tree
        tree_str = ""
        directories = []
        files = []

        for key, subtree in current.items():
            if subtree == 'file':
                files.append(key)
            else:
                directories.append(key)

        for directory in sorted(directories):
            tree_str += f"{prefix}|___{directory}/\n"
            tree_str += self.display(current[directory], prefix + '    ')
        
        for file in sorted(files):
            tree_str += f"{prefix}|___{file}\n"
            
        return tree_str

    def reset(self):
        self.tree = {}

# ErrorLogger와 DirectoryTree 인스턴스 생성
error_logger = ErrorLogger(log_dir=os.path.join(os.path.dirname(__file__), "ERROR_LOGS"))
directory_tree = DirectoryTree()

# URL 테스트 함수
async def test_url(session, test_url, progress, semaphore, skipped_urls, checked_urls, word, retry_with_extensions=False):
    max_retries = 5
    retry_delay = 1
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:91.0) Gecko/20100101 Firefox/91.0'
    }

    if test_url in skipped_urls or test_url in checked_urls:
        return

    async with semaphore:
        for attempt in range(max_retries):
            try:
                async with session.get(test_url, headers=headers) as response:
                    if response.status == 0:
                        error_message = "서버 응답 없음 (status=0)"
                        skipped_urls.add(test_url)
                        await error_logger.log_error(test_url, "ClientResponseError", error_message)
                        return
                    await handle_response(response, test_url, progress, word, retry_with_extensions)
                checked_urls.add(test_url)
                return
            except aiohttp.ClientResponseError as e:
                error_message = f"ClientResponseError: {e}"
                print(f"Response error on {test_url}: {error_message}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "ClientResponseError", error_message)
                return
            except aiohttp.ClientConnectorError as e:
                error_message = f"ClientConnectorError: {e}"
                print(f"Connection error on {test_url}: {error_message}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "ClientConnectorError", error_message)
                return
            except aiohttp.ClientSSLError as e:
                error_message = f"ClientSSLError: {e}"
                print(f"SSL error on {test_url}: {error_message}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "ClientSSLError", error_message)
                return
            except aiohttp.ClientOSError as e:
                error_message = f"ClientOSError: {e}"
                print(f"OS error on {test_url}: {error_message}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "ClientOSError", error_message)
                return
            except asyncio.TimeoutError:
                error_message = "TimeoutError"
                print(f"Timeout error on {test_url}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "TimeoutError", error_message)
                return
            except Exception as e:
                error_message = f"Unexpected error: {e}"
                print(f"Unexpected error on {test_url}: {error_message}")
                skipped_urls.add(test_url)
                await error_logger.log_error(test_url, "UnexpectedError", error_message)
                return

# 응답 상태 코드 처리 함수
async def handle_response(response, test_url, progress, word, retry_with_extensions):
    if response.status == 200:
        # 200번대 응답인 경우 디렉토리로 간주하고 처리
        is_directory = test_url.endswith('/')
        is_file = '.' in urlparse(test_url).path.split('/')[-1] and not is_directory
        progress['vulnerable_tests'] += 1
        progress['vulnerable_words'].append((word, test_url))
        progress['existing_paths'].append(test_url)
        directory_tree.add_path(urlparse(test_url).path, is_file)
        
        # 파일로 간주된 경우에만 확장자를 붙여서 재시도
        if not is_file and not retry_with_extensions and not is_directory:
            common_extensions = ['.php', '.html', '.asp', '.aspx', '.jsp', '.txt', '.xml', '.json']
            for ext in common_extensions:
                full_url_with_ext = f"{test_url.rstrip('/')}{ext}"
                await test_url_with_extension(full_url_with_ext, word + ext, progress)
    else:
        # 200번대가 아닌 응답은 로그에 기록하고 오류로 간주
        error_message = f"{response.status} - {response.reason}"
        await error_logger.log_error(test_url, f"Status Code {response.status}", error_message)

# 확장자를 붙인 URL을 테스트하는 함수
async def test_url_with_extension(full_url_with_ext, word_with_ext, progress):
    async with aiohttp.ClientSession() as session:
        async with session.get(full_url_with_ext) as response:
            if response.status == 200:
                is_file = '.' in urlparse(full_url_with_ext).path.split('/')[-1]
                if is_file:
                    progress['vulnerable_tests'] += 1
                    progress['vulnerable_words'].append((word_with_ext, full_url_with_ext))
                    progress['existing_paths'].append(full_url_with_ext)
                    directory_tree.add_path(urlparse(full_url_with_ext).path, is_file)

# 디렉토리 구조를 탐색하는 함수
async def fuzz_directory(target_url, txt_files, start_time):
    directory_tree.reset()
    progress = {
        'total_tests': 0,
        'vulnerable_tests': 0,
        'vulnerable_words': [],
        'existing_paths': []
    }
    semaphore_value = 10
    max_requests = 1000
    skipped_urls = set()
    checked_urls = set()

    for txt_file in txt_files:
        filepath = os.path.join('./wordlist', txt_file)
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            total_lines = len(lines)
            if total_lines > 1000000:
                semaphore_value = 7
                max_requests = 700
            else:
                semaphore_value = 10
                max_requests = 1000

    semaphore = asyncio.Semaphore(semaphore_value)
    connector = aiohttp.TCPConnector(limit_per_host=semaphore_value)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=120)) as session:
        tasks = []
        for txt_file in txt_files:
            filepath = os.path.join('./wordlist', txt_file)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                for line in lines:
                    words = line.strip().split()
                    for word in words:
                        full_url = f"{target_url}/{word}"
                        if full_url not in checked_urls:
                            tasks.append(test_url(session, full_url, progress, semaphore, skipped_urls, checked_urls, word))
                        full_url_slash = f"{target_url}/{word}/"
                        if full_url_slash not in checked_urls:
                            tasks.append(test_url(session, full_url_slash, progress, semaphore, skipped_urls, checked_urls, word + '/'))
                        progress['total_tests'] += 1
                        if len(tasks) >= max_requests:
                            await asyncio.gather(*tasks)
                            tasks = []
        if tasks:
            await asyncio.gather(*tasks)

    error_logger.write_error_logs()
    error_logger.close_error_log_file()

    parsed_url = urlparse(target_url)
    domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
    
    filtered_vulnerable_words = [(word, url) for word, url in progress['vulnerable_words'] if url.endswith('/') or '.' in urlparse(url).path.split('/')[-1]]

    final_filtered_words = []
    seen_paths = set()

    for word, url in filtered_vulnerable_words:
        path = urlparse(url).path
        base_path = path.rstrip('/').rsplit('.', 1)[0]
        if base_path in seen_paths:
            final_filtered_words = [(w, u) for w, u in final_filtered_words if not (urlparse(u).path.rstrip('/') == base_path and '/' in urlparse(u).path)]
        seen_paths.add(base_path)
        final_filtered_words.append((word, url))

    directory_tree.reset()
    for _, url in final_filtered_words:
        path = urlparse(url).path
        is_file = '.' in path.split('/')[-1]
        directory_tree.add_path(path, is_file)

    tree_str = domain + '\n' + directory_tree.display()
    vulnerable_tests = len(final_filtered_words)
    progress['vulnerable_tests'] = vulnerable_tests

    print(f"\n검출된 테스트/전체 테스트 : {vulnerable_tests}/{progress['total_tests']}")
    print("디렉터리 트리 구조:")
    print(tree_str)
    
    report_path = generate_report(target_url, txt_files, progress['total_tests'], vulnerable_tests, final_filtered_words, start_time)
    return os.path.basename(report_path), progress['total_tests'], vulnerable_tests, final_filtered_words, tree_str

# 테스트 보고서 생성 함수
def generate_report(target_url, txt_files, total_tests, vulnerable_tests, vulnerable_words, start_time):
    end_time = datetime.now()
    timestamp = end_time.strftime("%Y%m%d_%H%M%S")
    report_name = f"report_{timestamp}.txt"
    report_content = f"테스트 시작 시각: {start_time}\n"
    report_content += f"테스트 종료 시각: {end_time}\n"
    report_content += f"테스트 대상 URL: {target_url}\n"
    report_content += f"테스트 파일 목록: {', '.join(txt_files)}\n"
    report_content += f"테스트 한 단어 수: {total_tests}\n"
    report_content += f"발견된 디렉터리 수: {vulnerable_tests}\n"
    report_content += "검출된 디렉터리 목록:\n"
    report_content += "\n".join(f"{word}" for word in vulnerable_words)

    report_path = os.path.join(os.path.dirname(__file__), "REPORT")
    if not os.path.exists(report_path):
        os.makedirs(report_path)

    report_file = os.path.join(report_path, report_name)
    with open(report_file, 'w') as f:
        f.write(report_content)

    print(f"보고서가 '{report_file}' 경로에 저장되었습니다.")
    return report_file

# TXT 파일 목록 가져오는 함수
def get_txt_file_list():
    txt_dir = './wordlist'
    txt_files = []
    for root, dirs, files in os.walk(txt_dir):
        for file in files:
            if file.endswith(".txt"):
                relative_path = os.path.relpath(os.path.join(root, file), txt_dir)
                txt_files.append(relative_path)
    return txt_files

# URL 유효성 검사 함수
def validate_url(url):
    parsed_url = urlparse(url)
    
    # 프로토콜이 없는 경우 http를 붙임
    if not parsed_url.scheme:
        url = 'http://' + url
        parsed_url = urlparse(url)

    if not parsed_url.netloc:
        return False, "유효하지 않은 URL입니다. 올바른 URL을 입력해주세요."

    try:
        response = requests.head(url, timeout=5)
        if response.status_code >= 500:
            return False, "서버에서 응답을 받지 못했습니다. URL을 확인해주세요."
    except requests.RequestException:
        return False, "존재하지 않는 URL입니다."

    return True, url

# 디렉토리 트리를 렌더링하는 함수
def render_tree(tree):
    def render_node(name, node):
        if isinstance(node, dict):
            directories = {k: v for k, v in node.items() if isinstance(v, dict)}
            files = {k: v for k, v in node.items() if not isinstance(v, dict)}
            items = "".join([render_node(k, v) for k, v in sorted(directories.items())])
            items += "".join([render_node(k, v) for k, v in sorted(files.items())])
            return f'<li class="directory"><span>{name}</span><ul>{items}</ul></li>'
        else:
            return f'<li class="file"><span>{name}</span></li>'

    tree_html = "".join([render_node(name, node) for name, node in tree.items()])
    return Markup(f'<ul>{tree_html}</ul>')

# URL 유효성 검사를 위한 API 엔드포인트
@app.route('/validate_url', methods=['POST'])
def api_validate_url():
    data = request.json
    url = data.get('url')

    valid, message = validate_url(url)
    return jsonify({"valid": valid, "message": message})

# 메인 페이지 라우트
@app.route('/', methods=['GET', 'POST'])
def index():
    error_logger.reset_logs()
    if request.method == 'POST':
        target_url = request.form['target_url']
        txt_files = request.form.getlist('txt_file')
        start_time = datetime.now()
        result_filename, total_tests, vulnerable_tests, vulnerable_words, tree_str = asyncio.run(fuzz_directory(target_url, txt_files, start_time))
        return render_template('result.html', total_tests=total_tests, vulnerable_tests=vulnerable_tests, vulnerable_words=vulnerable_words, report_filename=result_filename, tree=directory_tree.tree, render_tree=render_tree, target_url=target_url)
    return render_template('index.html', txt_files=get_txt_file_list())

# 보고서 다운로드 라우트
@app.route('/download/<report_filename>')
def download_report(report_filename):
    report_path = os.path.join(os.path.dirname(__file__), "REPORT", report_filename)
    return send_file(report_path, as_attachment=True)

# 결과 페이지 라우트
@app.route('/result/<report_filename>')
def result(report_filename):
    return render_template('result.html', report_filename=report_filename)

# API 라우트 - TXT 파일 목록
@app.route('/api/txt_files')
def api_txt_files():
    return jsonify(get_txt_file_list())

if __name__ == "__main__":
    app.run(host='0.0.0.0', debug=True)
