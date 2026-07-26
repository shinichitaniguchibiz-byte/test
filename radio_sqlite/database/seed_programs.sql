INSERT INTO schema_version(version_no, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'))
ON CONFLICT(version_no) DO UPDATE SET applied_at = excluded.applied_at;

INSERT INTO program (
    program_id, program_name, program_url, is_active,
    output_directory, directory_name, program_abbreviation,
    english_title, site_id, corner_id, created_at, updated_at
) VALUES
(
    1,
    'エンジョイ・シンプル・イングリッシュ',
    'https://www.nhk.or.jp/radio/ondemand/detail.html?p=BR8Z3NX7XM_01',
    1, 'audio', 'シンプル英語', 'ESE',
    'Enjoy Simple English', 'BR8Z3NX7XM', '01',
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'),
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
),
(
    2,
    'ラジオビジネス英語',
    'https://www.nhk.jp/p/radio-bizeigo/rs/368315KKP8/list/',
    1, 'audio', 'ビジネス英語', 'RBE',
    'Radio Business English', '368315KKP8', '01',
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'),
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
),
(
    3,
    'ニュースで学ぶ「現代英語」',
    'https://www.nhk.jp/p/gendaieigo/rs/77RQWQX1L6/list/',
    1, 'audio', '現代英語', 'NME',
    'Learn English from the News', '77RQWQX1L6', '01',
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'),
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
),
(
    4,
    'ラジオ英会話',
    'https://www.nhk.jp/p/rs/PMMJ59J6N2/list/',
    1, 'audio', 'ラジオ英会話', 'REC',
    'Radio English Conversation', 'PMMJ59J6N2', '01',
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'),
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
),
(
    5,
    '英会話タイムトライアル',
    'https://www.nhk.jp/p/rs/8Z6XJ6J415/list/',
    1, 'audio', 'タイムトライアル', 'ETT',
    'English Conversation Time Trial', '8Z6XJ6J415', '01',
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours'),
    strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
)
ON CONFLICT(program_id) DO UPDATE SET
    program_name = excluded.program_name,
    program_url = excluded.program_url,
    is_active = excluded.is_active,
    output_directory = excluded.output_directory,
    directory_name = excluded.directory_name,
    program_abbreviation = excluded.program_abbreviation,
    english_title = excluded.english_title,
    site_id = excluded.site_id,
    corner_id = excluded.corner_id,
    updated_at = excluded.updated_at;
