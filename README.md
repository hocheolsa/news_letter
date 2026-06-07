# News Dashboard

GitHub Actions와 GitHub Pages로 운영하는 규칙 기반 뉴스 대시보드입니다.

## 핵심 변경점

- Gemini API를 사용하지 않습니다.
- 카카오톡 발송 기능을 사용하지 않습니다.
- RSS와 Google News RSS에서 최근 24시간 이내 기사만 수집합니다.
- 카테고리별 키워드, 우선 키워드, 제외어, 전문매체/기관/기업 공식 소스 가산점, 최신성 점수는 `config/news_config.json`에서 관리합니다.
- 기사 본문 추출이 가능한 경우 본문을 참고해 한줄요약을 만들고, 실패 시 제목·RSS 요약 기반의 로컬 요약으로 대체합니다.
- URL, 정규화 제목, 이슈 키를 함께 사용해 중복 기사를 줄입니다.
- 해외 기사 제목은 외부 번역 API 없이 주요 뉴스 용어 사전으로 가볍게 보정합니다.
- 수집 결과는 `docs/data/news_latest.json`에 저장되고 `docs/index.html`이 이를 표시합니다.

## 수집 기준

- 실행 시점 기준 최근 24시간 기사만 포함합니다.
- 발행 시각이 없거나 파싱할 수 없는 기사는 제외합니다.
- 발행 시각은 KST ISO 8601 형식의 `published_at`으로 저장합니다.
- 주요 섹터는 `AI`, `Tech`, `사이버보안`, `국내 경제·증시`, `국제 경제·증시`로 구성합니다.
- `Tech`는 기존 양자·양자컴퓨터·반도체·메모리·클라우드 인프라 성격의 뉴스를 통합합니다.
- 주요 섹터는 각 5건, 증시 산업별은 8개 카테고리별 4건을 목표로 합니다.
- 목표 건수 미달 그룹은 해당 기사에 `shortage: true`를 표시합니다.

## 데이터 구조

```json
{
  "generated_at": "2026-06-06T15:30:00+09:00",
  "window": {
    "from": "2026-06-05T15:30:00+09:00",
    "to": "2026-06-06T15:30:00+09:00",
    "timezone": "Asia/Seoul"
  },
  "totals": {
    "expected": 57,
    "collected": 62,
    "shortage_groups": 0
  },
  "items": []
}
```

## 로컬 실행

```bash
pip install -r requirements.txt
python scripts/collect_news.py
```

생성된 결과는 `docs/data/news_latest.json`에 저장됩니다.

## 운영 설정

뉴스 소스, 카테고리, 키워드, 차단 매체, 출처 신뢰도, 점수 가중치는 `config/news_config.json`에서 수정합니다.

주요 설정:

- `direct_feeds`: 공식 기관, 전문 매체, 금융 매체 RSS
- `source_tiers`: 공식/통신/전문/금융/일반 출처 등급
- `groups`: 섹터별 검색 쿼리, 포함 키워드, 우선 키워드, 목표 건수
- `article_extraction.max_articles_per_group`: 본문 추출 대상 상위 후보 수

## 자동 실행

`.github/workflows/collect.yml`은 6시간마다 실행됩니다.

수동 업데이트는 GitHub 저장소의 Actions 탭에서 `collect-news` 워크플로를 `Run workflow`로 실행합니다. 웹페이지의 `수동 업데이트` 버튼은 GitHub Pages 배포 환경에서 해당 워크플로 화면을 엽니다.

## Gemini API 선택 연동

기본값은 Gemini 비활성화입니다. Gemini API가 없어도 로컬 수집/선별/요약으로 정상 동작합니다.

Gemini를 사용하려면 GitHub 저장소에서 아래 값을 설정합니다.

- `Settings > Secrets and variables > Actions > Secrets`
  - `GEMINI_API_KEY`: 발급받은 Gemini API 키
- `Settings > Secrets and variables > Actions > Variables`
  - `ENABLE_GEMINI`: `true`
  - `GEMINI_MODEL`: `gemini-2.5-flash-lite`

Gemini 호출은 `config/news_config.json`의 `gemini` 설정으로 제한됩니다.

- 실행당 최대 호출: `max_requests_per_run`
- 일일 최대 호출: `max_requests_per_day`
- 호출 간 대기: `min_seconds_between_requests`
- 후보 기사 수: `max_candidates_per_group`
- 본문 전달 길이: `max_body_chars`

API 오류, 한도 초과, 인증 오류, 서버 오류가 발생하면 해당 실행에서는 Gemini 호출을 즉시 중지하고 로컬 요약으로 계속 진행합니다.

## 수동 업데이트 비밀번호

웹페이지의 수동 업데이트 버튼은 `docs/index.html`의 아래 값을 확인합니다.

```js
const MANUAL_UPDATE_PASSWORD = "CHANGE_ME_MANUAL_PASSWORD";
```

원하는 값으로 바꿔서 사용하세요. 이 값은 정적 페이지 코드에 노출되므로 강한 보안 수단이 아니라, 버튼 오작동을 막는 용도입니다. 실제 수동 실행은 GitHub 로그인과 저장소 권한으로 보호됩니다.
