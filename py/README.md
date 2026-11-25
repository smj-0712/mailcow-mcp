# mailcow-mcp

Mailcow 서버를 Claude 등 MCP 클라이언트에서 다루는 MCP 서버입니다.
[maximeoliv/mcp-mailcow](https://github.com/maximeoliv/mcp-mailcow)(MIT)를 포크해 Python 구현만 남기고 별도로 유지하는 프로젝트입니다.

> **stdio**(기본)와 **Streamable HTTP**(`--transport http`, `/mcp`) 전송을 지원합니다.
> HTTP 모드는 Bearer 토큰이 없으면 시작하지 않고, 기본적으로 읽기 전용 도구만 노출합니다.

## 기능

도구 정의는 [`tools-schema.yaml`](tools-schema.yaml)에 있고 총 125개입니다. 두 가지 모드로 실행합니다.

### user 모드 (IMAP/SMTP, 21개)
메일박스 하나를 대상으로 합니다.
- 읽기: 받은편지함 목록, 메시지 읽기, 원문, 검색, 안 읽은 수, 첨부 다운로드
- 보내기: 새 메일, 답장, 전달, 임시보관
- 폴더: 목록, 생성, 이름 변경, 삭제, 비우기
- 플래그/이동: 읽음/안 읽음, 별표, 커스텀 플래그, 이동, 삭제

### admin 모드 (Mailcow REST API, 104개)
- 도메인, 메일박스(용량 리포트, 태그, ACL 포함), 별칭, 시간제한 별칭, 앱 비밀번호
- DKIM
- 메일 라우팅: BCC 맵, 수신자 맵, transport, relayhost, TLS 정책, forward host
- imapsync 작업, 리소스, OAuth2 클라이언트
- 도메인 관리자, 도메인 정책(블랙/화이트리스트), 격리함
- 전달 테스트(`send_test_mail`): 서버 호스트에서 docker가 필요합니다

삭제 계열 도구와 `mailbox_set_password`는 `confirm: true`가 있어야 실행됩니다.
생성/수정 도구는 확인 없이 바로 적용되니 주의하세요.

각 도구에는 `tools-schema.yaml`의 `readonly: true|false` 분류가 붙어 있습니다
(user 21개 중 읽기 7개, admin 104개 중 읽기 35개). 이 분류는
[쓰기 도구 게이트](#쓰기-도구-게이트)에서 사용합니다.

## 설치와 실행

Python 3.10 이상이 필요합니다. 아래 `uvx` 예시는 [uv](https://docs.astral.sh/uv/)를 쓰고,
uv 없이 실행하려면 [`.env` 만들고 `python main.py`](#env-만들고-python-mainpy)를 보세요.

```bash
git clone https://github.com/smj-0712/mailcow-mcp.git
```

Claude Code에 등록하는 예시(admin 모드)입니다. 경로는 절대경로를 쓰세요.

```bash
claude mcp add -s user mailcow-admin \
  -e MAILCOW_ADMIN_URL=https://mail.example.com \
  -e MAILCOW_ADMIN_API_KEY=발급한키 \
  -- uvx --from /path/to/mailcow-mcp/py mcp-mailcow --mode admin
```

user 모드는 환경변수와 `--mode`만 바꿉니다.

```bash
claude mcp add -s user mailcow-mail \
  -e MAILCOW_HOST=mail.example.com \
  -e MAILCOW_MAIL_USER=you@example.com \
  -e MAILCOW_MAIL_PASS=앱비밀번호 \
  -- uvx --from /path/to/mailcow-mcp/py mcp-mailcow --mode user
```

명령 이름과 패키지 이름은 아직 원본과 같은 `mcp-mailcow`입니다.

### `.env` 만들고 `python main.py`

저장소 루트의 `main.py`는 `py/src`를 import 경로에 넣고 `mcp_mailcow.__main__.main()`을 호출합니다.
패키지를 설치하지 않아도 되고 uv도 필요 없습니다. 실행 전에 `main.py`와 같은 폴더의 `.env`를 읽습니다.

```bash
cd mailcow-mcp
pip install -r requirements.txt
cp .env.example .env      # 값을 실제 값으로 바꾸세요
python main.py
```

`.env.example`에 모든 변수와 설명이 있고, 값은 전부 가짜 예시입니다. `.env`는 `.gitignore`에 들어 있습니다.
`requirements.txt`는 `py/pyproject.toml`의 의존성과 같은 목록입니다.

설정 우선순위는 **CLI 플래그 > 실제 환경변수 > `.env` > 기본값**입니다.

- `.env`는 이미 있는 환경변수를 덮어쓰지 않습니다. 값이 빈 문자열이어도 "이미 있음"으로 봅니다.
  예를 들어 패널에서 `MCP_MAILCOW_AUTH_TOKEN`을 빈 값으로 정의해 두면 `.env`의 토큰은 쓰이지 않습니다.
- `.env` 문법은 `KEY=VALUE`, 줄 전체 `#` 주석, `'...'`/`"..."` 따옴표만 지원합니다.
  따옴표 없는 값에서는 공백 뒤의 `#`부터 주석으로 처리합니다.
  `export`, 변수 치환(`${VAR}`), 이스케이프, 여러 줄 값은 지원하지 않습니다.
- 형식이 잘못된 줄은 파일 이름과 줄 번호만 경고하고 건너뜁니다. 값은 출력하지 않습니다.
- `.env`가 없으면 환경변수와 기본값만 씁니다.

`--mode`는 선택 인자입니다. 없으면 `MCP_MAILCOW_MODE`(`user` 또는 `admin`)를 쓰고,
둘 다 없으면 무엇이 필요한지 출력하고 종료 코드 2로 끝납니다. 기존 플래그는 그대로 동작합니다.

```bash
python main.py --mode admin                    # 플래그로 지정 (stdio)
python main.py --mode admin --transport http   # .env보다 플래그가 우선
```

설치된 `mcp-mailcow` 명령은 `.env`를 읽지 않고 환경변수와 플래그만 씁니다.

### HTTP 전송

| 옵션 | 기본값 | 환경변수 |
|---|---|---|
| `--mode user\|admin` | 없음(필수) | `MCP_MAILCOW_MODE` |
| `--transport stdio\|http` | `stdio` | `MCP_MAILCOW_TRANSPORT` |
| `--host` | `0.0.0.0` | `MCP_MAILCOW_HOST` |
| `--port` | `8000` | `MCP_MAILCOW_PORT`, 없으면 `SERVER_PORT` |

엔드포인트는 두 개입니다.

- `/mcp`: MCP Streamable HTTP 엔드포인트. stateless 모드로 동작하고 결과를 JSON으로 돌려줍니다.
  세션을 메모리에 두지 않으므로 서버를 재시작해도 클라이언트가 세션을 다시 만들 필요가 없습니다.
- `/health`: 인증 없이 `{"status": "ok"}`를 돌려줍니다. Mailcow에는 요청하지 않습니다.

HTTP로 쓰려면 `.env`에 최소한 다음을 넣습니다.

```dotenv
MCP_MAILCOW_MODE=admin
MCP_MAILCOW_TRANSPORT=http
MCP_MAILCOW_AUTH_TOKEN=긴임의문자열
MAILCOW_ADMIN_URL=https://mail.example.com
MAILCOW_ADMIN_API_KEY=발급한키
```

토큰은 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 등으로 만드세요.
Claude Code에 HTTP 서버로 등록하는 예시입니다.

```bash
claude mcp add --transport http mailcow-admin https://mcp.example.com/mcp \
  --header "Authorization: Bearer 긴임의문자열"
```

### Pterodactyl (python egg)

서버 파일에 저장소를 두고 `.env`를 만든 뒤, 시작 명령을 다음으로 둡니다.

```bash
python main.py
```

의존성은 `pip install -r requirements.txt`로 미리 설치해 둬야 합니다. egg가 자동으로 설치하지 않는다면
시작 명령을 `pip install -r requirements.txt && python main.py`로 두세요.
`.env` 대신 패널의 시작 변수(환경변수)로 같은 변수를 넘겨도 되고, 이때는 시작 변수가 `.env`보다 우선합니다.

포트는 `MCP_MAILCOW_PORT`가 없으면 `SERVER_PORT` 환경변수를 씁니다.
Pterodactyl Wings 소스(`server/server.go`의 `GetEnvironmentVariables`, develop 브랜치)에서는
컨테이너에 `SERVER_PORT=<기본 할당 포트>`를 넣는 것을 확인했습니다. 다만 실제 Pterodactyl 패널/노드에서
실행해 본 것은 아니므로, 포트가 맞지 않으면 `.env`나 시작 변수에 `MCP_MAILCOW_PORT`를 직접 지정하세요.

## 환경변수

### user 모드
| 변수 | 필수 | 설명 |
|---|---|---|
| `MAILCOW_HOST` | O | Mailcow 호스트명 |
| `MAILCOW_MAIL_USER` | O | 메일 주소 전체 |
| `MAILCOW_MAIL_PASS` | O | 앱 비밀번호 권장 |
| `MAILCOW_IMAP_HOST`, `MAILCOW_SMTP_HOST` | | 기본값은 `MAILCOW_HOST` |
| `MAILCOW_IMAP_PORT` | | 기본 `993` |
| `MAILCOW_SMTP_PORT` | | 기본 `587` |

### admin 모드
| 변수 | 필수 | 설명 |
|---|---|---|
| `MAILCOW_ADMIN_URL` | O | 예: `https://mail.example.com` |
| `MAILCOW_ADMIN_API_KEY` | O | Mailcow API 키 |
| `MCP_MAILCOW_API_TIMEOUT` | | 요청 타임아웃(초), 기본 `60` |

### 공통
| 변수 | 기본값 | 설명 |
|---|---|---|
| `MCP_MAILCOW_AUDIT_LOG` | `~/.local/state/mcp-mailcow/audit.log` | 감사 로그 경로(JSONL) |
| `MCP_MAILCOW_TLS_VERIFY` | `true` | `false`면 TLS 검증을 끕니다. 자체 서명 인증서에만 쓰세요 |
| `MCP_MAILCOW_ENABLE_WRITE_TOOLS` | stdio: `true`, http: `false` | 쓰기 도구 사용 여부. 아래 [쓰기 도구 게이트](#쓰기-도구-게이트) 참고 |

### 전송/HTTP
| 변수 | 기본값 | 설명 |
|---|---|---|
| `MCP_MAILCOW_MODE` | 없음 | `user` 또는 `admin` (`--mode`의 기본값). 플래그와 둘 다 없으면 시작하지 않습니다 |
| `MCP_MAILCOW_TRANSPORT` | `stdio` | `stdio` 또는 `http` (`--transport`의 기본값) |
| `MCP_MAILCOW_HOST` | `0.0.0.0` | HTTP 바인드 주소 (`--host`의 기본값) |
| `MCP_MAILCOW_PORT` | `SERVER_PORT`, 없으면 `8000` | HTTP 포트 (`--port`의 기본값) |
| `SERVER_PORT` | 없음 | `MCP_MAILCOW_PORT`가 없을 때 쓰는 포트. Pterodactyl이 기본 할당 포트로 설정합니다 |
| `MCP_MAILCOW_AUTH_TOKEN` | 없음 | HTTP 모드의 Bearer 토큰. 없으면 HTTP 서버가 시작하지 않습니다 |
| `MCP_MAILCOW_ALLOW_NO_AUTH` | `false` | `true`일 때만 토큰 없이 HTTP 서버를 시작합니다(시작 시 경고 로그) |
| `MCP_MAILCOW_ALLOWED_HOSTS` | 없음 | 쉼표로 구분한 허용 Host 헤더 목록(`host:*`로 포트 와일드카드). 설정하면 다른 Host는 421로 거부 |
| `MCP_MAILCOW_ALLOWED_ORIGINS` | 없음 | `MCP_MAILCOW_ALLOWED_HOSTS`를 설정했을 때 허용할 브라우저 Origin 목록. Origin 헤더가 없는 요청은 통과 |

`MCP_MAILCOW_ALLOWED_HOSTS`를 비워 두면 Host 헤더를 검사하지 않습니다. 리버스 프록시가
공개 도메인을 Host로 넘겨도 막히지 않고, 접근 제어는 Bearer 토큰이 맡습니다.

## 인증 (HTTP)

- `MCP_MAILCOW_AUTH_TOKEN`이 있으면 `/mcp` 요청에 `Authorization: Bearer <토큰>` 헤더가 있어야 합니다.
  헤더가 없거나 토큰이 다르면 401을 돌려줍니다. 토큰 비교는 `hmac.compare_digest`로 합니다.
  `/health`는 인증 대상이 아닙니다.
- 토큰 없이 `--transport http`로 시작하면 이유를 출력하고 종료 코드 2로 끝납니다.
  다른 수단(사설망, 인증 프록시)으로 접근이 막혀 있을 때만 `MCP_MAILCOW_ALLOW_NO_AUTH=true`를 쓰세요.
- 토큰과 API 키 값은 로그, 에러 메시지, 감사 로그에 남기지 않습니다. `.env`에서 읽은 값도 마찬가지입니다.
- TLS는 이 서버가 처리하지 않습니다. 외부에 공개할 때는 HTTPS 리버스 프록시 뒤에 두세요.

## 쓰기 도구 게이트

`MCP_MAILCOW_ENABLE_WRITE_TOOLS`가 `false`이면 `tools-schema.yaml`에서 `readonly: true`가 아닌 도구를
`tools/list`에서 숨기고, 이름을 알고 호출해도 핸들러를 실행하지 않고 거부합니다. 거부한 호출은 감사 로그에
`"result": "denied"`로 남습니다.

- 기본값: stdio는 `true`(기존 동작 그대로), http는 `false`.
- `readonly: false`에는 상태를 바꾸는 도구 외에 메일 발송(`send_message`, `send_test_mail` 등), 자격 증명
  발급·변경(`app_password_create`, `mailbox_set_password`, `domain_admin_sso_token` 등), 응답에 비밀값이 들어갈 수
  있는 조회(`transport_list`, `relayhost_list`, `sync_job_list`, `oauth2_client_list`, `logs_get_api`)도 포함됩니다.
- `read_message`는 읽기 도구지만 `mark_read: true`로 호출하면 `\Seen` 플래그를 바꾸므로 거부합니다
  (스키마의 `write_params`).
- `readonly` 필드가 없는 도구는 쓰기 도구로 취급합니다.

## 보안 권장 사항
- admin API 키는 사실상 서버 전체 권한입니다. 조회만 할 때는 읽기 전용 키를 쓰세요.
- user 모드는 메인 비밀번호 대신 앱 비밀번호를 쓰세요.
- 도구 호출은 감사 로그에 기록되고, 비밀번호/키/토큰 값은 마스킹됩니다.
- HTTP 모드에서는 쓰기 도구가 꼭 필요할 때만 `MCP_MAILCOW_ENABLE_WRITE_TOOLS=true`를 켜세요.

## 개발

```bash
cd py
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .[dev]
pytest -v
ruff check src
mypy src/mcp_mailcow
```

루트의 `tools-schema.yaml`이 도구 정의의 원본이고, `py/src/mcp_mailcow/tools-schema.yaml`에 같은 내용이 복사돼 있습니다. 도구를 바꾸면 두 파일을 함께 갱신해야 합니다(테스트가 두 파일이 같은지, 모든 도구에 `readonly`가 있는지 확인합니다).
의존성을 바꾸면 `py/pyproject.toml`과 루트 `requirements.txt`를 함께 고치세요.

## 라이선스

MIT. 원저작자 Maxime Olivier의 저작권 고지를 [LICENSE](LICENSE)에 유지합니다.
