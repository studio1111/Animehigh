import unittest

from app import (allowed_media_url, episode_details, parse_episode_feed, parse_feed, safe_filename, telegram_method_for_file)


class FeedParserTests(unittest.TestCase):
    def test_parses_episode_page_and_poster(self):
        xml = '''<rss><channel><item><title>Moonlit Saga Episode 07</title><link>https://anime.example.org/moonlit/episode-7</link><enclosure url="https://cdn.example.org/poster.jpg" type="image/jpeg"/></item></channel></rss>'''
        self.assertEqual(parse_episode_feed(xml), [{
            "title": "Moonlit Saga Episode 07",
            "url": "https://anime.example.org/moonlit/episode-7",
            "image_url": "https://cdn.example.org/poster.jpg",
        }])

    def test_extracts_episode_number_without_guessing(self):
        self.assertEqual(episode_details("Moonlit Saga Episode 07"), ("Moonlit Saga", "07"))
        self.assertEqual(episode_details("青い星 第12話"), ("青い星", "12"))
        self.assertEqual(episode_details("Moonlit Saga latest update"), ("Moonlit Saga latest update", ""))

    def test_parses_rss_enclosure(self):
        xml = '''<?xml version="1.0"?><rss><channel><item><title>Episode 01</title><enclosure url="https://media.example.org/ep01.mkv" type="video/x-matroska"/><link>https://example.org/post</link></item></channel></rss>'''
        self.assertEqual(parse_feed(xml), [{"title": "Episode 01", "url": "https://media.example.org/ep01.mkv"}])

    def test_parses_atom_enclosure(self):
        xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Episode 02</title><link href="https://example.org/post"/><link rel="enclosure" href="https://media.example.org/ep02.mp4"/></entry></feed>'''
        self.assertEqual(parse_feed(xml)[0]["url"], "https://media.example.org/ep02.mp4")

    def test_rejects_page_torrent_magnet_and_unknown_host(self):
        allowed = ["media.example.org"]
        for url in (
            "https://media.example.org/watch/ep01",
            "https://media.example.org/ep01.torrent",
            "magnet:?xt=urn:btih:abc",
            "https://evil.example/ep01.mkv",
            "http://media.example.org/ep01.mkv",
        ):
            with self.subTest(url=url):
                self.assertFalse(allowed_media_url(url, allowed))

    def test_accepts_direct_media_on_exact_allowlisted_host(self):
        self.assertTrue(allowed_media_url("https://media.example.org/ep01.mkv", ["media.example.org"]))
        self.assertFalse(allowed_media_url("https://sub.media.example.org/ep01.mkv", ["media.example.org"]))

    def test_filename_is_sanitized(self):
        self.assertEqual(safe_filename("Episode / 01?", "https://media.example.org/video.mkv"), "Episode _ 01.mkv")

    def test_telegram_uses_video_for_mp4_and_document_for_mkv(self):
        self.assertEqual(telegram_method_for_file("episode.mp4"), "sendVideo")
        self.assertEqual(telegram_method_for_file("episode.MP4"), "sendVideo")
        self.assertEqual(telegram_method_for_file("episode.mkv"), "sendDocument")
        self.assertEqual(telegram_method_for_file("episode.webm"), "sendDocument")
