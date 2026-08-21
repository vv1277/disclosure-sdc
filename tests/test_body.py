"""본문 파일 식별과 디코딩 (전량 파싱에서 발견한 문제들)."""
import io
import zipfile

import pytest

from src.parse.body import decode_bytes, pick_body_file

BODY = "<?xml version='1.0' encoding='utf-8'?><DOCUMENT><P>II. 사업의 내용</P></DOCUMENT>"
ATTACH = "<?xml version='1.0'?><DOCUMENT><DOCUMENT-NAME>연결감사보고서</DOCUMENT-NAME></DOCUMENT>"


def _zip(tmp_path, members: dict[str, bytes]):
    p = tmp_path / "t.zip"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return p


def test_declared_encoding_is_not_trusted():
    """XML 선언이 utf-8 이라고 써 놓고 실제로는 cp949 인 파일이 있다."""
    raw = "<?xml version='1.0' encoding='utf-8'?><P>사업의 내용</P>".encode("cp949")
    assert "사업의 내용" in decode_bytes(raw)


def test_two_bad_bytes_do_not_destroy_the_whole_file():
    """1.9MB 중 2바이트만 깨져도 strict 디코딩은 실패한다.

    '처음 성공하는 인코딩' 방식이면 올바른 cp949 를 건너뛰고
    utf-8 errors=replace 로 떨어져 9.5% 가 통째로 깨진다.
    실제로 하나투어 2020 이 그랬고 섹션 헤더를 하나도 못 찾았다.
    치환문자가 가장 적은 인코딩을 골라야 한다.
    """
    good = ("<?xml version='1.0' encoding='utf-8'?>"
            + "<P>사업의 내용</P>" * 200).encode("cp949")
    corrupted = good[:100] + b"\xff\xfe" + good[102:]
    text = decode_bytes(corrupted)
    assert text.count("사업의 내용") > 190
    assert text.count("\ufffd") <= 4


def test_zip_with_only_attachment_is_not_mistaken_for_body(tmp_path):
    """첨부정정 문서의 ZIP 에는 감사보고서만 들어 있는 경우가 있다."""
    z = _zip(tmp_path, {"a_00761.xml": ATTACH.encode("cp949"),
                        "b.xml": BODY.encode("cp949")})
    name, text = pick_body_file(z)
    assert "사업의 내용" in text
    assert name == "b.xml"


def test_larger_file_without_body_markers_loses_to_smaller_one(tmp_path):
    """가장 큰 파일이 본문이라는 보장이 없다. 섹션명이 든 파일을 우선한다."""
    big = ("<?xml version='1.0'?><DOCUMENT>" + "<P>표만 있음</P>" * 500
           + "</DOCUMENT>").encode("cp949")
    z = _zip(tmp_path, {"big.xml": big, "small.xml": BODY.encode("cp949")})
    name, text = pick_body_file(z)
    assert name == "small.xml"


def test_raises_when_no_candidate(tmp_path):
    z = _zip(tmp_path, {"a.txt": b"x"})
    with pytest.raises(ValueError, match="본문 후보 없음"):
        pick_body_file(z)
