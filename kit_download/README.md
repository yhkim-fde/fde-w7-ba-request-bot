# 강의 실습 데이터

FDE Academy 7주차 A트랙 강의에서 배포한 실습 자료입니다. 챗봇이 바로 읽는 폴더 구조로 넣어 두었습니다. 누리전자는 과정상 가명이고, 개인정보(이름·전화번호)는 배포 단계에서 마스킹된 데이터입니다.

| 자료 | 위치 | 쓰는 곳 |
|---|---|---|
| DDL 35개 | `kit_download/ddl/A-schema/DDL/*.sql` | 챗봇 실행 (로컬 DB 만들 때 테이블 구조) |
| 마스킹 데이터 CSV 35개 + 데이터 안내 | `kit_download/masked/A-db/` | 챗봇 실행 (미리 실행용 로컬 DB) |
| 대시보드 캡처 · 패널 쿼리 4개 | `kit_download/dashboard/` | `tests/evalset.py`의 정답 비교 |

```
kit_download/
├── ddl/A-schema/DDL/                          ← BOT_DDL_DIR 기본값
│   └── LX_BNF1_M.sql … TMP_KJH_0915.sql (35개)
├── masked/A-db/
│   ├── 데이터_안내.txt
│   └── 마스킹_추출본_20261120/                  ← BOT_DATA_DIR 기본값 (A-db 아래 첫 폴더)
│       └── LX_BNF1_M.csv … TMP_KJH_0915.csv (35개)
└── dashboard/
    ├── A-dash/대시보드_캡처_20261101.png
    └── A-dashsql/                             ← tests/evalset.py 가 읽는 곳
        └── 패널1~4_*.sql
```

- 챗봇을 처음 실행하면 CSV를 읽어 `draft_chatbot/var/nuri_masked.db`(로컬 SQLite, Redshift 대역)를 만듭니다. 이 파일은 커밋되지 않습니다.
- 다른 위치의 데이터를 쓰려면 환경 변수로 지정합니다: `BOT_DDL_DIR=<DDL 폴더>` `BOT_DATA_DIR=<CSV 폴더>`
