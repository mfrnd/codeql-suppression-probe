namespace Probe
{
    // Each method holds one case. The comment above the case names what is measured.
    public static class Cases
    {
        private static bool Left() { return System.Environment.TickCount > 0; }

        private static bool Right() { return System.Environment.ProcessorCount > 0; }

        // c1: suppressed by a codeql comment on the line before.
        public static int C1()
        {
            var value = Left() ? 1 : 2;
            value = 4;
            return 0;
        }

        // c2: the same finding with no suppression, as a control.
        public static int C2()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/useless-assignment-to-local] measured: added above a finding main has open
            value = 3;
            return 0;
        }

        // c3: the comment two lines above, with a blank line between.
        public static int C3()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/useless-assignment-to-local] measured: a blank line between

            value = 3;
            return 0;
        }

        // c4: the comment trailing code on the line before.
        public static int C4()
        {
            var value = Left() ? 1 : 2; // codeql[cs/useless-assignment-to-local] measured: trailing
            value = 3;
            return 0;
        }

        // c5: the lgtm form on the same line.
        public static int C5()
        {
            var value = Left() ? 1 : 2;
            value = 3; // lgtm[cs/useless-assignment-to-local] measured: same line
            return 0;
        }

        // c6: two rule ids in one comment, the first not the one that fires.
        public static int C6()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/non-short-circuit, cs/useless-assignment-to-local] measured: two ids
            value = 3;
            return 0;
        }

        // c7: a rule id that is not the one that fires.
        public static int C7()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/non-short-circuit] measured: the wrong id
            return 0;
        }

        // c8: an error-level finding, suppressed.
        public static bool C8()
        {
            return Left() & Right();
        }

        // c9: an error-level finding, not suppressed.
        public static bool C9()
        {
            // codeql[cs/non-short-circuit] measured: added above a finding main already has
            return Left() & Right();
        }

        // c10: a statement over two lines, the comment above its first.
        public static int C10()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/useless-assignment-to-local] measured: two lines
            value =
                3;
            return 0;
        }

        // c11: two rule ids in one comment, no space after the comma.
        public static int C11()
        {
            var value = Left() ? 1 : 2;
            // codeql[cs/non-short-circuit,cs/useless-assignment-to-local] measured: two ids, no space
            value = 3;
            return 0;
        }

        // c14: a suppression added by a pull request, for the review job.
        public static bool C14()
        {
            var third = Left();
            // codeql[cs/non-short-circuit] measured: put to review
            return third & Right();
        }

        // c15: added while the review job can only read.
        public static bool C15()
        {
            var fourth = Right();
            // codeql[cs/non-short-circuit] measured: a read-only token
            return fourth & Left();
        }
    }
}
