# litellm-copilot-gateway 작업 규칙

GitHub Copilot 구독 모델을 Claude Code에서 쓰는 로컬 게이트웨이 (litellm :4000 → copilot-api :4141 → api.githubcopilot.com). 아키텍처·검증 현황·알려진 제약은 README.md 참고. 이 파일은 이 repo에서 작업할 때 지켜야 할 규칙만 담는다.

## 절대 규칙

- **`config.yaml`을 직접 수정하지 말 것** — `refresh-models.sh`가 전체를 재생성하는 자동 생성 파일(git-ignored)이다. 라우팅/설정을 바꾸려면 `refresh-models.sh`의 생성 로직을 수정하고 재실행할 것.
- **litellm 업그레이드 후 반드시 `./apply-patches.sh` 실행** — 업그레이드가 로컬 패치 4종을 지운다. 각 패치는 앵커 텍스트 매칭이라 업스트림 코드가 바뀌면 실패 메시지가 뜬다(그때 수동 점검).
- **small/fast 모델(gpt-4o-mini, haiku 별칭)의 capi(:4141) 라우팅을 깨지 말 것** — litellm의 github_copilot 직결 경로로 돌리면 Claude Code WebSearch가 400으로 죽는다 (README "WebSearch 동작 방식" 참고).
- **명시적인 모델 이름을 다른 모델로 리다이렉트하지 말 것** — `model_catalog.py`는 현재 picker 모델만 공개하고 동일 모델의 표기 차이·원본 ID·지원되는 `[1m]`만 숨김 별칭으로 둔다. 종료된 모델은 오류로 거부. 과거의 Fable/구버전 → Opus 5, Haiku → GPT 대체는 제거되었다.
- **copilot-api는 검증된 2.6.15 고정** — 변경 시 `start-proxy.sh`와 `start-copilot-api.py`의 버전 및 실제 배포 번들 패치 호환성을 함께 확인. 모델 목록 새로고침은 버전 고정과 별개이며, 앵커 검증이나 모델 정체성 보호를 끄지 말 것.
- `.env`(마스터키)와 크레덴셜 파일은 절대 커밋 금지 (.gitignore에 있음).

## 자주 쓰는 작업

`claude-copilot.sh`(= `ccp`)는 실행할 때마다 자동으로 `refresh-models.sh && restart-proxy.sh`를 먼저 돌린다 (실패 시 기존 config.yaml로 폴백하되 두 서비스 readiness는 확인). 아래는 Claude Code를 띄우지 않고 게이트웨이만 조작할 때 쓴다.

```bash
./restart-proxy.sh                          # litellm만 재시작 (config 리로드)
./refresh-models.sh && ./restart-proxy.sh   # 모델 목록 갱신 + 적용
tail -f proxy.log                           # litellm 로그 (에러는 여기 먼저)
tail -f copilot-api.log                     # copilot-api 로그
```

## 변경 후 검증

먼저 `PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True ~/.local/share/uv/tools/litellm/bin/python -m unittest discover -s tests -v` 실행. 설치본 패치는 임시 사본에서 검증하고 사용자 토큰·프로세스는 테스트 더블로 대체한다.

아래 curl 스모크는 유료 추론을 포함하므로 명시적으로 요청된 경우에만 실행. 그 외에는 두 서비스 readiness와 `/v1/models`의 공개 모델·메타데이터·숨김 별칭을 확인한다.

`source .env` 후, `-H "Authorization: Bearer $LITELLM_MASTER_KEY"`로 `http://127.0.0.1:4000/v1/messages`에:

1. **WebSearch** (가장 잘 깨지는 경로): `"model":"claude-haiku-4-5"`, `"tools":[{"type":"web_search_20250305","name":"web_search"}]`, `"tool_choice":{"type":"tool","name":"web_search"}` — 응답에 `server_tool_use` + `web_search_tool_result` + 실검색 결과가 있어야 정상.
2. **스트리밍**: `"stream":true`로 claude-sonnet-5와 현재 공개 GPT 모델 각각 — `message_stop`으로 끝나고 "list index out of range"가 없어야 정상.
3. **메인 모델 회귀**: claude-opus-5 단순 질문 200 확인.

문제가 :4000에서만 나면 litellm, :4141 직접 호출(인증 불필요)에서도 나면 copilot-api/업스트림 문제로 분리 진단.

## 트러블슈팅 메모

- `stop-proxy.sh`/`restart-proxy.sh`는 포트 소유 PID와 실행 파일·인자를 확인한 뒤 그 PID에만 SIGTERM을 보낸다. 다른 경로/설정의 프로세스는 거부하므로 `lsof -iTCP:4000` 또는 `lsof -iTCP:4141`로 소유자를 확인. `pkill`/`killall`이나 무조건적인 강제 종료로 우회하지 말 것.
- proxy.log의 `Anthropic CountTokens API error: 401` 경고는 무해 (로컬 토크나이저로 폴백, 요청은 200).
- WSL에서는 `jq` 정적 바이너리가 `~/.local/bin`에 있음.
