from app.services.economy import level_for_xp, valid_message_score


def test_level_increases_slowly():
    assert level_for_xp(0) == 1
    assert level_for_xp(119) == 1
    assert level_for_xp(120) == 2


def test_spam_detection():
    assert valid_message_score("hola, como estan?")[0]
    assert not valid_message_score("aaaaaaaaaaaa")[0]
