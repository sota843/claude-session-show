"""Timing/state tests for band_anim (no drawing, no Win32)."""
import unittest

from band_anim import (COLOR_SECS, GLINT_SECS, SHIMMER_PERIOD, SPARKLE_FPS, TWEEN_FPS,
                       BandAnimator)

OK, WARN, CRIT, UNKNOWN = (0, 200, 0), (200, 200, 0), (200, 0, 0), (128, 128, 128)


def color_of(p):
    if p is None:
        return UNKNOWN
    return CRIT if p >= 0.85 else WARN if p >= 0.6 else OK


class BandAnimatorTest(unittest.TestCase):
    def make(self, **kw):
        a = BandAnimator(color_of, 0.85, **kw)
        a.retarget([0.40, 0.50], 0.0)
        return a

    def test_first_reading_is_shown_without_animation(self):
        a = self.make(sparkle=False, shimmer=False)
        self.assertFalse(a.active(0.0))
        self.assertEqual([f.pct for f in a.frame(0.0)], [0.40, 0.50])

    def test_bar_eases_to_new_value_and_settles(self):
        a = self.make(sparkle=False, shimmer=False)
        a.retarget([0.50, 0.50], 10.0)
        mid = a.frame(10.2)[0].pct
        self.assertTrue(0.40 < mid < 0.50)
        self.assertAlmostEqual(a.frame(12.0)[0].pct, 0.50)
        self.assertFalse(a.active(12.0))

    def test_retarget_mid_tween_continues_from_shown_value(self):
        a = self.make()
        a.retarget([0.60, 0.50], 10.0)
        shown = a.frame(10.1)[0].pct
        a.retarget([0.30, 0.50], 10.1)
        self.assertAlmostEqual(a.frame(10.1)[0].pct, shown)

    def test_glint_after_percent_changes_but_not_for_same_value(self):
        a = self.make(sparkle=False, shimmer=False)
        a.retarget([0.40, 0.50], 10.0)
        self.assertFalse(a.active(10.0))
        a.retarget([0.45, 0.50], 10.0)
        glints = [a.frame(10.0 + i / 30)[0].glint for i in range(60)]
        self.assertTrue(any(g is not None for g in glints))
        self.assertIsNone(a.frame(10.0 + 2)[0].glint)

    def test_threshold_crossing_crossfades_colour(self):
        a = self.make()
        a.retarget([0.70, 0.50], 10.0)
        mid = a.frame(10.0 + COLOR_SECS / 2)[0].color
        self.assertNotIn(mid, (OK, WARN))
        self.assertEqual(a.frame(12.0)[0].color, WARN)

    def test_reset_drains_slower_than_small_change(self):
        a = self.make()
        a.retarget([0.95, 0.50], 0.0)
        small = a.rows[0].dur
        a.retarget([0.0, 0.50], 5.0)
        self.assertGreater(a.rows[0].dur, small)
        self.assertGreater(a.frame(5.3)[0].pct, 0.0)

    def test_breathing_only_for_crit_rows_and_not_when_stale(self):
        a = self.make(sparkle=False, shimmer=False)
        a.retarget([0.90, 0.50], 0.0)
        later = 5.0 + 0.6  # tween over, breath not at its peak
        self.assertTrue(a.active(later))
        self.assertLess(a.frame(later)[0].pulse, 1.0)
        self.assertEqual(a.frame(later)[1].pulse, 1.0)
        self.assertFalse(a.active(later, pulse_ok=False))
        self.assertEqual(a.frame(later, pulse_ok=False)[0].pulse, 1.0)

    def test_disabled_switches_instantly(self):
        a = self.make(enabled=False, pulse=False, sparkle=False, shimmer=False)
        a.retarget([0.90, 0.50], 10.0)
        self.assertEqual(a.frame(10.0)[0].pct, 0.90)
        self.assertFalse(a.active(10.0))

    def test_unknown_values_switch_instantly(self):
        a = self.make(sparkle=False, shimmer=False)
        a.retarget([None, 0.50], 10.0)
        self.assertIsNone(a.frame(10.0)[0].pct)
        a.retarget([0.30, 0.50], 11.0)
        self.assertEqual(a.frame(11.0)[0].pct, 0.30)
        self.assertFalse(a.active(11.0 + GLINT_SECS))


class SparkleTest(unittest.TestCase):
    def sample(self, a, secs=60.0, **kw):
        return [a.sparkles(t / SPARKLE_FPS, **kw) for t in range(int(secs * SPARKLE_FPS))]

    def make(self, pcts, **kw):
        a = BandAnimator(color_of, 0.85, **{"shimmer": False, **kw})
        a.retarget(pcts, 0.0)
        return a

    def test_sparkles_keep_coming_while_idle(self):
        frames = self.sample(self.make([0.70, 0.50]))
        lit = sum(1 for f in frames if f)
        self.assertGreater(lit, len(frames) * 0.3)    # something twinkles often ...
        self.assertLess(lit, len(frames))             # ... but not non-stop
        for f in frames:
            for sp in f:
                self.assertTrue(0 < sp.x < 1 and 0 < sp.y < 1 and 0 <= sp.size <= 1)

    def test_more_sparkles_on_a_longer_fill(self):
        short = sum(len(f) for f in self.sample(self.make([0.10, None])))
        full = sum(len(f) for f in self.sample(self.make([0.95, None])))
        self.assertGreater(full, short * 1.5)
        self.assertTrue(all(sp.row == 0 for f in self.sample(self.make([0.95, None]))
                            for sp in f))

    def test_none_when_empty_stale_or_off(self):
        self.assertFalse(any(self.sample(self.make([0.0, None]))))
        a = self.make([0.70, 0.50])
        self.assertFalse(any(self.sample(a, live=False)))
        self.assertFalse(a.active(1.0, pulse_ok=False))
        off = self.make([0.70, 0.50], sparkle=False, shimmer=False)
        self.assertFalse(any(self.sample(off)))
        self.assertIsNone(off.next_delay(1.0))

    def test_deterministic_so_frames_agree(self):
        a, b = self.make([0.70, 0.50]), self.make([0.70, 0.50])
        self.assertEqual(self.sample(a, 10), self.sample(b, 10))

    def test_sleeps_between_twinkles_but_never_past_a_second(self):
        a = self.make([0.70, 0.50])
        for t in range(int(60 * SPARKLE_FPS)):
            now = t / SPARKLE_FPS
            d = a.next_delay(now)
            self.assertLessEqual(d, 1.0)
            if a.sparkles(now):
                self.assertAlmostEqual(d, 1 / SPARKLE_FPS)
            else:
                # nothing lights up before the promised wake-up
                self.assertFalse(a.sparkles(now + d * 0.99))


class ShimmerTest(unittest.TestCase):
    def make(self, pcts, **kw):
        a = BandAnimator(color_of, 0.85, **{"sparkle": False, **kw})
        a.retarget(pcts, 0.0)
        return a

    def glints(self, a, row, secs, **kw):
        return [a.frame(t / TWEEN_FPS, **kw)[row].glint for t in range(int(secs * TWEEN_FPS))]

    def test_shine_sweeps_again_every_period(self):
        a = self.make([0.70, 0.50])
        g = self.glints(a, 0, SHIMMER_PERIOD * 3)
        starts = [i for i in range(1, len(g)) if g[i] is not None and g[i - 1] is None]
        self.assertEqual(len(starts), 2)  # plus the one already running at t=0
        self.assertAlmostEqual((starts[1] - starts[0]) / TWEEN_FPS, SHIMMER_PERIOD, places=1)
        self.assertTrue(all(0 <= x < 1 for x in g if x is not None))
        self.assertGreater(sum(x is None for x in g), len(g) / 2)  # mostly at rest

    def test_reaches_7d_just_after_5h(self):
        a = self.make([0.70, 0.50])
        first = lambda row: next(i for i, x in enumerate(self.glints(a, row, SHIMMER_PERIOD * 2))
                                 if i and x is not None and x < 0.1)
        self.assertGreater(first(1), first(0))

    def test_none_when_empty_stale_off_or_no_battery(self):
        a = self.make([0.0, None])
        self.assertTrue(all(x is None for x in self.glints(a, 0, SHIMMER_PERIOD)))
        a = self.make([0.70, 0.50])
        self.assertTrue(all(x is None for x in self.glints(a, 0, SHIMMER_PERIOD, pulse_ok=False)))
        self.assertTrue(all(x is None for x in self.glints(a, 0, SHIMMER_PERIOD, fill_ok=False)))
        self.assertFalse(a.active(1.0, fill_ok=False))
        self.assertIsNone(self.make([0.70, 0.50], shimmer=False).next_delay(1.0))

    def test_sleeps_between_sweeps(self):
        a = self.make([0.70, 0.50])
        for t in range(int(SHIMMER_PERIOD * 2 * TWEEN_FPS)):
            now = t / TWEEN_FPS
            d = a.next_delay(now)
            self.assertLessEqual(d, 1.0)
            sweeping = any(f.glint is not None for f in a.frame(now))
            if sweeping:
                self.assertAlmostEqual(d, 1 / TWEEN_FPS)
            else:
                self.assertFalse(any(f.glint is not None for f in a.frame(now + d * 0.99)))

    def test_value_change_shine_still_shows_and_is_not_baked_into_cache_frames(self):
        a = self.make([0.40, 0.50])
        a.retarget([0.45, 0.50], 2.0)  # idle sweep is at rest around t=2..5
        self.assertTrue(any(x is not None for x in self.glints(a, 0, 3.5)[60:]))
        self.assertIsNotNone(a.frame(0.3)[1].glint)
        self.assertIsNone(a.frame(0.3, fill_ok=False)[1].glint)


if __name__ == "__main__":
    unittest.main()
