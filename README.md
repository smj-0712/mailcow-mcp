# mailcow-mcp

Mailcow 서버를 Claude 등 MCP 클라이언트에서 다루는 MCP 서버입니다.
[maximeoliv/mcp-mailcow](https://github.com/maximeoliv/mcp-mailcow)(MIT)를 포크해 Python 구현만 남기고 별도로 유지하는 프로젝트입니다.

> 현재는 **stdio 전송만** 지원합니다. HTTP 전송은 추가할 예정이며 아직 없습니다.

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

## 설치와 실행

Python 3.10 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다.

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

## 보안 권장 사항
- admin API 키는 사실상 서버 전체 권한입니다. 조회만 할 때는 읽기 전용 키를 쓰세요.
- user 모드는 메인 비밀번호 대신 앱 비밀번호를 쓰세요.
- 도구 호출은 감사 로그에 기록되고, 비밀번호/키/토큰 값은 마스킹됩니다.

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

루트의 `tools-schema.yaml`이 도구 정의의 원본이고, `py/src/mcp_mailcow/tools-schema.yaml`에 같은 내용이 복사돼 있습니다. 도구를 바꾸면 두 파일을 함께 갱신해야 합니다.

## 라이선스

MIT. 원저작자 Maxime Olivier의 저작권 고지를 [LICENSE](LICENSE)에 유지합니다.
