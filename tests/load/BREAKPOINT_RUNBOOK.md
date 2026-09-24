# 2026-09-22 breakpoint 시험 실행 기록

이 문서는 PostgreSQL `max_connections=100`인 로컬 환경에서 실행한 시험의
준비 조건과 단계별 명령을 기록한다. [계획](BREAKPOINT_TEST_PLAN.md)과
[결과](results/2026-09-22-breakpoint/summary.md)를 함께 읽는다.
제품 용량 보증이나 전체 시험을 자동 실행하는 스크립트는 아니다.

## 실행 도구와 범위

- `run_breakpoint.py`: 하나의 단계에서 목표 요청 시작률을 유지하고 응답을 정산한다.
- `breakpoint_contract.py`: 오류·지연·발생기 한계와 경계 탐색 기준을 계산한다.
- `breakpoint_fixture.py`, `breakpoint_mock.py`: Start → HTTP 10초 대기 → Answer를 만든다.
- `breakpoint_observer.py`, `breakpoint_probe/sitecustomize.py`: 관측과 중단 신호를 담당한다.
- `docker-compose.breakpoint.yml`: 시험 전용 mock과 경로를 구성한다.

실제 시험의 환경 전환, fixture 설치, 단계 연결, DB 대조, 원래 환경 복구는
당시의 로컬 임시 스크립트와 운영 명령으로 수행했다. 그 스크립트에는 호스트 경로,
해당 회차의 출력 디렉터리와 PID가 들어 있어 범용 도구로 커밋하지 않는다.
아래 명령은 준비를 마친 격리 환경에서 **한 단계**를 실행하는 명령이다.
새 환경에서 준비·회복 절차 없이 연속으로 실행하지 않는다.

## 당시 준비 절차

1. 일반 프로젝트 `docker`의 실제 이미지 ID, named volume, 실행 상태를 기록했다.
   큐·unacked·RUNNING이 비었는지 확인한 뒤 일반 컨테이너를 종료했다.
   고정 container name을 쓰므로 일반 환경과 시험 환경을 동시에 띄우지 않았다.
2. 기존 `manage_compose.py init`으로 생성한 비공개 환경 파일과
   `docker/docker-compose.yml`, `docker-compose.override.yml`,
   `docker-compose.breakpoint.yml`, 회차별 이미지·계측 overlay를 사용했다.
   프로젝트는 `nodease-loadtest`, API 입구는 `http://127.0.0.1:18080`이었다.
   기존 이미지로 `up -d --no-build --pull never --wait`를 수행했다.
   `docker/.env`와 시험용 환경 파일·manifest는 커밋하지 않는다.
3. 기존 seed/verify로 기본 template 워크플로우와 권한을 확인한 후,
   `build_io_workflow_graph(delay_seconds=10)`의 graph를 시험 manifest가 지정한
   API workflow와 그 앱의 활성 deployment snapshot에 함께 반영했다.
   컨테이너 소유 프로젝트, workflow의 조직, 활성 deployment가 하나인지 확인했다.
   기본 `manage_compose.py verify`는 template graph 검증용이므로 HTTP fixture 설치
   후의 검증을 대신하지 않는다. 설치 후에는 두 graph의 일치와 단건 응답을 확인했다.
4. Gateway와 Workflow Engine에 아래 설정과 bind mount를 시험 overlay로 적용했다.
   `sitecustomize.py`는 이 opt-in 설정이 있을 때만 계측한다.

   | 설정 | 값 |
   |---|---|
   | `NODEASE_BREAKPOINT_OBSERVER` | `1` |
   | `NODEASE_BREAKPOINT_SERVICE` | 해당 서비스 이름: `gateway` / `workflow_engine` |
   | `NODEASE_BREAKPOINT_EVENT_DIR` | `/breakpoint-events` |
   | `PYTHONPATH` | `/opt/nodease-breakpoint:/app` |
   | 읽기 전용 mount | `tests/load/breakpoint_probe` → `/opt/nodease-breakpoint` |
   | 쓰기 가능한 mount | 회차별 로컬 event 디렉터리 → `/breakpoint-events` |

5. collector를 시험 네트워크에 연결하고 Prometheus 수집·Redis·Loki·HTTP health와
   probe ready 이벤트를 확인했다. collector 설정과 dashboard는 기존 커밋을 사용했다.
   Docker 8 CPU / 약 7.65 GiB, PostgreSQL 연결 상한 100, workflow concurrency 100을
   유지했다. 시험 중 DB 상한이나 worker 수를 높이지 않았다.
6. mock 직접 검증은 18.75 RPS, 준비 10초 + 측정 120초였다.
   10초 workflow 단건 보정은 2분 동안 순차 실행했다. 이 둘이 통과한 뒤 A를 시작했다.

## 단계별 명령

저장소 루트에서 실행한다. `tests/load/.venv`는 기존 README의 의존성 설치를 따른다.
`bp_events`는 위 overlay의 로컬 event 디렉터리와 같아야 한다.
각 단계는 별도의 새 출력 디렉터리를 사용한다.

```bash
bp_output=tests/load/reports/new-breakpoint/A-baseline
bp_events=tests/load/reports/new-breakpoint/events
mkdir -p "$bp_output" "$bp_events"

# 터미널 1: 관측기를 먼저 실행한다.
tests/load/.venv/bin/python -m tests.load.breakpoint_observer \
  --events "$bp_events" --output "$bp_output/observations.jsonl" \
  --stop-file "$bp_output/stop" --done-file "$bp_output/done"
```

다른 터미널에서 같은 `bp_output`을 지정한다. 최신 observation의
`observation_available`, `mock_available`, `health_ok`가 참이고
`probe_ready_count > 0`이며 stop 파일이 없음을 확인한 뒤 실행한다.

```bash
bp_output=tests/load/reports/new-breakpoint/A-baseline
tests/load/.venv/bin/python -m tests.load.run_breakpoint \
  --output "$bp_output" --rate 0.5 --warmup 60 --duration 240 \
  --stop-file "$bp_output/stop"
```

실제로 진행한 단계는 다음과 같다. 실패 이후의 순서는 회복 확인에 의존하므로
표 전체를 무조건 실행하는 shell loop로 바꾸지 않는다.

| 단계 | RPS | 준비 / 측정 초 | 실제 종료 |
|---|---:|---:|---|
| A-baseline | 0.5 | 60 / 240 | 통과 |
| B1 | 1 | 60 / 180 | 통과 |
| B2 | 2.5 | 60 / 180 | 통과 |
| B3 | 5 | 60 / 180 예정 | 준비 구간 조기 중단 |
| E-after-B | 0.5 | 60 / 240 | 단건·큐 확인 후 회복 검증 통과 |
| C1 | 3.75 | 60 / 180 | 통과 |
| C2 | 4.375 | 60 / 180 | 오류율 초과 및 기록 정합성 회복 실패 |

## 중단·정산·복구

발생기 종료만으로 회복을 판정하지 않는다. 신규 요청 중단 시각을 기준으로 최대
600초 동안 HTTP 미완료, Redis의 workflow/log/knowledge/celery 우선순위 큐와 unacked,
worker active/reserved/scheduled, 시험 workflow의 DB 최종 상태를 대조한다.
HTTP 응답 run ID와 DB run ID는 SHA-256 해시로 대조하며 원본 입력·응답을 저장하지 않는다.
DB RUNNING과 실제 엔진 동시 실행을 같은 수치로 취급하지 않는다.

큐·unacked·실행 중 작업이 연속 두 번 0이고 정합성 누락이 없으면 단건 실행 및
0.5 RPS 회복 시험으로 넘어간다. 누락·잔류가 600초 뒤에도 남으면 후속 시험을 중단한다.
당시 임시 실행기의 drain은 요청 중단 시각보다 늦게 시작할 수 있어 별도 deadline
감시로 이 상한을 지켰다. 기록된 실제 판정 시점은 중단 후 600.165초다.

정산·증거 수집이 끝난 뒤 해당 단계의 `done` 파일을 생성해 관측기를 종료한다.
시험 프로젝트는 `down`으로 내리되 `-v`를 사용하지 않는다. 일반 환경은 원래
이미지·volume을 기준으로 복원하고 collector를 일반 네트워크에 재연결한다.
Nodease 페이지·API health·Grafana 응답, 최신 수집 상태, 기존 DB volume을 확인한다.
당시 proxy 이미지 교체 예외와 복구 결과는 결과 보고서에 기록했다.

## 이번 커밋의 검증 범위

이전 spike 실행기 의존성만 제거했고, 동일한 `requests.Session()`의
`trust_env=False` 동작은 유지했다. 제외한 파일을 import하지 않는 회귀 테스트를
먼저 실패시킨 뒤 변경했다. 문서·결과 선별은 코드 동작 변경이 아니므로 TDD 대상이 아니다.
제품의 인증·권한·DB schema·실행 로직을 바꾸지 않아 보호 리소스 기능 완결성 검토는
이번 정리 작업에 적용하지 않는다. 새 부하 실행이나 DB 연결 고갈 결함 수정은 하지 않았다.

2026-09-24 커밋 준비 검증: 아래 5개 테스트 파일에서 **91 passed**, 기존 경고 10개.
9월 22일의 `tool-validation.json`은 당시의 90 passed 기록을 그대로 유지했다.
전체 저장소 회귀·원격 CI는 실행하지 않았다.

```bash
PYTHONPATH=. apps/gateway/.venv/bin/python -m pytest -q \
  tests/ci/test_breakpoint_contract.py tests/ci/test_breakpoint_fixture.py \
  tests/ci/test_breakpoint_observer.py tests/ci/test_breakpoint_probe.py \
  tests/ci/test_breakpoint_runner.py
```
