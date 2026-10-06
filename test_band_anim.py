"""Timing/state tests for band_anim (no drawing, no Win32)."""
import unittest

from band_anim import COLOR_SECS, GLINT_SECS, BandAnimator

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
        a = self.make()
        self.assertFalse(a.active(0.0))
        self.assertEqual([f.pct for f in a.frame(0.0)], [0.40, 0.50])

    def test_bar_eases_to_new_value_and_settles(self):
        a = self.make()
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
        a = self.make()
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
        a = self.make()
        a.retarget([0.90, 0.50], 0.0)
        later = 5.0 + 0.6  # tween over, breath not at its peak
        self.assertTrue(a.active(later))
        self.assertLess(a.frame(later)[0].pulse, 1.0)
        self.assertEqual(a.frame(later)[1].pulse, 1.0)
        self.assertFalse(a.active(later, pulse_ok=False))
        self.assertEqual(a.frame(later, pulse_ok=False)[0].pulse, 1.0)

    def test_disabled_switches_instantly(self):
        a = self.make(enabled=False, pulse=False)
        a.retarget([0.90, 0.50], 10.0)
        self.assertEqual(a.frame(10.0)[0].pct, 0.90)
        self.assertFalse(a.active(10.0))

    def test_unknown_values_switch_instantly(self):
        a = self.make()
        a.retarget([None, 0.50], 10.0)
        self.assertIsNone(a.frame(10.0)[0].pct)
        a.retarget([0.30, 0.50], 11.0)
        self.assertEqual(a.frame(11.0)[0].pct, 0.30)
        self.assertFalse(a.active(11.0 + GLINT_SECS))


if __name__ == "__main__":
    unittest.main()
