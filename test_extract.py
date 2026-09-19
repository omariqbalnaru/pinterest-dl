#!/usr/bin/env python3
"""Offline tests for pinterest-dl extraction logic. Run: python3 test_extract.py"""

import json
import unittest

import pinterest_dl as pdl


def cdn(size, digest, ext):
    return f"https://i.pinimg.com/{size}/{digest[:2]}/{digest[2:4]}/{digest[4:6]}/{digest}.{ext}"


A = "a" * 32
B = "b" * 32
C = "c" * 32


class TestClassify(unittest.TestCase):
    def test_plain_pin(self):
        kind, pid, canon = pdl.classify("https://www.pinterest.com/pin/123456789012345678/")
        self.assertEqual((kind, pid), ("pin", "123456789012345678"))
        self.assertEqual(canon, "https://www.pinterest.com/pin/123456789012345678/")

    def test_feedback_pin_with_query(self):
        """The /feedback/ variant must still resolve to the bare pin path."""
        url = ("https://www.pinterest.com/pin/123456789012345678/feedback/"
               "?invite_code=abcdef01&sender_id=123456789012345678")
        kind, pid, canon = pdl.classify(url)
        self.assertEqual((kind, pid), ("pin", "123456789012345678"))
        self.assertEqual(canon, "https://www.pinterest.com/pin/123456789012345678/")

    def test_slug_pin(self):
        kind, pid, _ = pdl.classify(
            "https://www.pinterest.com/pin/example-pin-slug--123456789012345678/")
        self.assertEqual((kind, pid), ("pin", "123456789012345678"))

    def test_multi_pin_keeps_query(self):
        """Multi-pin links need their invite_code, so the query must survive."""
        url = ("https://www.pinterest.com/multi-pin-share/1234567890123456789/"
               "?invite_code=abcdef01&sender=123456789012345678")
        kind, pid, canon = pdl.classify(url)
        self.assertEqual(kind, "multi-pin-share")
        self.assertEqual(pid, "1234567890123456789")
        self.assertIn("invite_code=abcdef01", canon)

    def test_unknown(self):
        self.assertEqual(pdl.classify("https://example.com/x")[0], "unknown")


class TestModeSelection(unittest.TestCase):
    """The tool must pick its own mode: static where the HTML holds the pins,
    browser where it does not. A multi-pin link should never need a flag."""

    def test_multi_pin_requires_browser(self):
        self.assertTrue(pdl.needs_browser("multi-pin-share"))

    def test_pin_does_not_require_browser(self):
        self.assertFalse(pdl.needs_browser("pin"))
        self.assertFalse(pdl.needs_browser("unknown"))

    def test_short_link_resolving_to_multi_pin_requires_browser(self):
        """pin.it links are opaque until resolved; once resolved to a
        multi-pin share, browser mode must be selected automatically."""
        resolved = ("https://www.pinterest.com/multi-pin-share/1234567890123456789/"
                    "?invite_code=abcdef01")
        kind, _, _ = pdl.classify(resolved)
        self.assertTrue(pdl.needs_browser(kind))


class TestNoise(unittest.TestCase):
    def test_rejects_non_pin_assets(self):
        for url in [
            cdn("upload", A, "jpg"),
            "https://i.pinimg.com/retention-1days/mps_1234567890123456789.jpg",
            "https://i.pinimg.com/videos/thumbnails/originals/aa/bb/cc/x.jpg",
        ]:
            self.assertTrue(pdl._is_noise(url), url)

    def test_accepts_real_pin_image(self):
        self.assertFalse(pdl._is_noise(cdn("originals", A, "png")))

    def test_css_background_noise_not_collected(self):
        """Pinterest embeds its own assets in inline CSS; those must not be
        mistaken for the pin's image."""
        html = ("<style>:root{--x:url(" + cdn("originals", C, "png") +
                ")}</style>")
        self.assertEqual(pdl.collect_images(html), [])


class TestExtraction(unittest.TestCase):
    def test_jsonld_original_wins(self):
        html = ('<script type="application/ld+json">'
                '{"@type":"Pin","image":"' + cdn("originals", A, "png") + '"}</script>')
        imgs = pdl.collect_images(html)
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0].digest, A)
        self.assertEqual(imgs[0].source, "json-ld")
        self.assertEqual(pdl.candidate_urls(imgs[0], "originals")[0], cdn("originals", A, "png"))

    def test_og_image(self):
        html = ('<meta content="' + cdn("736x", A, "jpg") + '" property="og:image"/>')
        imgs = pdl.collect_images(html)
        self.assertEqual([i.digest for i in imgs], [A])

    def test_original_extension_probe_order(self):
        """The original's extension differs from the thumbnail's, so we must
        probe candidate extensions starting with the known one."""
        img = pdl.Image(url=cdn("736x", A, "jpg"), digest=A,
                        hash_path=f"{A[:2]}/{A[2:4]}/{A[4:6]}/{A}",
                        ext="jpg", size="736x")
        urls = pdl.candidate_urls(img, "originals")
        self.assertEqual(urls[0], cdn("originals", A, "jpg"))
        self.assertIn(cdn("originals", A, "png"), urls)

    def test_weak_sources_ignored_when_strong_present(self):
        """Related pins arrive via <link rel=preload>; they must not pollute a
        single pin's results when structured metadata exists."""
        html = (f'<link rel="preload" as="image" href="{cdn("736x", B, "jpg")}"/>'
                f'<script type="application/ld+json">'
                f'{{"image":"{cdn("originals", A, "png")}"}}</script>')
        imgs = pdl.collect_images(html)
        self.assertEqual([i.digest for i in imgs], [A])

    def test_weak_fallback_when_nothing_structured(self):
        html = f'<img src="{cdn("564x", B, "jpg")}"/>'
        imgs = pdl.collect_images(html)
        self.assertEqual([i.digest for i in imgs], [B])

    def test_best_size_wins_per_digest(self):
        html = (f'<img src="{cdn("236x", A, "jpg")}"/>'
                f'<img src="{cdn("originals", A, "png")}"/>')
        imgs = pdl.collect_images(html)
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0].size, "originals")

    def test_multi_pin_api_payload(self):
        """Browser mode feeds captured API JSON through extra_json; every pin
        in a multi-pin share must surface, deduped and at originals quality."""
        a_orig, a_736 = cdn("originals", A, "png"), cdn("736x", A, "jpg")
        b_orig, c_736 = cdn("originals", B, "jpg"), cdn("736x", C, "jpg")
        body = (
            '{"data":{"pins":['
            f'{{"id":"p1","images_orig":{{"url":"{a_orig}"}},'
            f'"images_736x":{{"url":"{a_736}"}}}},'
            f'{{"id":"p2","images_orig":{{"url":"{b_orig}"}}}},'
            f'{{"id":"p3","images_orig":null,'
            f'"images_736x":{{"url":"{c_736}"}}}}]}}'
        )
        imgs = pdl.collect_images("<html></html>", extra_json=[body])
        self.assertEqual(sorted(i.digest for i in imgs), sorted([A, B, C]))
        self.assertEqual({i.digest: i.source for i in imgs}[B], "originals")
        self.assertEqual({i.digest: i.size for i in imgs}[C], "736x")

    def test_quality_flag_honoured(self):
        img = pdl.Image(url=cdn("originals", A, "png"), digest=A,
                        hash_path=f"{A[:2]}/{A[2:4]}/{A[4:6]}/{A}",
                        ext="png", size="originals",
                        variants={"originals": cdn("originals", A, "png"),
                                  "736x": cdn("736x", A, "jpg")})
        self.assertEqual(pdl.candidate_urls(img, "736x")[0], cdn("736x", A, "jpg"))
        self.assertEqual(pdl.candidate_urls(img, "originals")[0], cdn("originals", A, "png"))


class TestPinListPayload(unittest.TestCase):
    """Multi-pin shares are resolved from the captured `pins` array. Board and
    related covers appear in *other* payloads on the same page and must not be
    mistaken for the shared pins."""

    def _share_body(self, pins):
        return json.dumps({"resource_response": {"data": {"pins": pins}}})

    def _recommendation_body(self):
        """Body 0 in a real capture: a recommendations rail whose cover images
        are not one of the shared pins."""
        return json.dumps({"resource_response": {"data": [{
            "objects": [{"id": "rec1", "cover_images": [
                {"236x": {"url": cdn("236x", C, "jpg")}},
                {"750x": {"url": cdn("750x", C, "jpg")}},
            ]}],
        }]}})

    def test_pins_array_is_authoritative(self):
        """The regression: a recommendations payload on the same page used to
        add a 4th image to a 3-pin share."""
        pins = [
            {"id": "111", "title": "First", "images": {"orig": {"url": cdn("originals", A, "png")}}},
            {"id": "222", "title": "Second", "images": {"orig": {"url": cdn("originals", B, "jpg")}}},
            {"id": "333", "images": {"736x": {"url": cdn("736x", C, "jpg")}}},
        ]
        imgs = pdl.collect_images(
            "<html></html>",
            extra_json=[self._recommendation_body(), self._share_body(pins)],
        )
        self.assertEqual(len(imgs), 3)                    # not 4
        self.assertEqual([i.pin_id for i in imgs], ["111", "222", "333"])
        self.assertEqual([i.title for i in imgs], ["First", "Second", ""])
        self.assertEqual(imgs[0].source, "api")

    def test_orig_preferred_over_thumbnail(self):
        pin = {"id": "1", "images": {
            "236x": {"url": cdn("236x", A, "jpg")},
            "orig": {"url": cdn("originals", A, "png")},
        }}
        imgs = pdl.collect_images("<html></html>", extra_json=[self._share_body([pin])])
        self.assertEqual(imgs[0].size, "originals")

    def test_title_in_dict_form(self):
        pin = {"id": "1", "title": {"text": "Nested Title"},
               "images": {"orig": {"url": cdn("originals", A, "jpg")}}}
        imgs = pdl.collect_images("<html></html>", extra_json=[self._share_body([pin])])
        self.assertEqual(imgs[0].title, "Nested Title")

    def test_pin_without_images_skipped(self):
        pins = [{"id": "1", "images": {}},
                {"id": "2", "images": {"orig": {"url": cdn("originals", A, "jpg")}}}]
        imgs = pdl.collect_images("<html></html>", extra_json=[self._share_body(pins)])
        self.assertEqual([i.pin_id for i in imgs], ["2"])

    def test_falls_back_when_nothing_usable(self):
        """A payload with no pins and no images must not suppress extraction
        from the HTML itself."""
        imgs = pdl.collect_images(
            f'<img src="{cdn("564x", A, "jpg")}"/>', extra_json=["{}"])
        self.assertEqual([i.digest for i in imgs], [A])

    def test_malformed_json_ignored(self):
        imgs = pdl.collect_images(
            f'<img src="{cdn("564x", A, "jpg")}"/>', extra_json=["{not json"])
        self.assertEqual([i.digest for i in imgs], [A])

    def test_api_images_outrank_dom_thumbnails(self):
        """Documents the precedence that makes the pin-list check necessary:
        without an explicit `pins` array, any pinimg URL in a captured payload
        is taken at face value — including unrelated cover images."""
        imgs = pdl.collect_images(
            f'<img src="{cdn("564x", A, "jpg")}"/>',
            extra_json=[self._recommendation_body()],
        )
        self.assertEqual([i.digest for i in imgs], [C])


class TestNaming(unittest.TestCase):
    def test_slug_sanitised_and_trimmed(self):
        html = ('<script type="application/ld+json">'
                '{"headline":"Example Pin Title, With Punctuation!"}</script>')
        title = pdl.page_title(html)
        self.assertEqual(pdl.re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-"),
                         "Example-Pin-Title-With-Punctuation")

    def test_title_falls_back_to_og(self):
        html = '<meta content="Some Pin Title" property="og:title"/>'
        self.assertEqual(pdl.page_title(html), "Some Pin Title")


class TestImageValidation(unittest.TestCase):
    def test_magic_bytes(self):
        self.assertTrue(pdl._looks_like_image(b"\xff\xd8\xff\xe0..."))
        self.assertTrue(pdl._looks_like_image(b"\x89PNG\r\n\x1a\n..."))
        self.assertTrue(pdl._looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 "))
        self.assertFalse(pdl._looks_like_image(b"<!DOCTYPE html><html>"))

    def test_ext_detection(self):
        self.assertEqual(pdl._ext_for(b"\x89PNG\r\n\x1a\n", "jpg"), "png")
        self.assertEqual(pdl._ext_for(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "jpg"), "webp")

    def test_html_error_page_rejected(self):
        """A 200 carrying an HTML error page must not be saved as an image."""
        self.assertFalse(pdl._looks_like_image(b"<html>not found</html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
