import unittest

from xhs_cli.mathtex import latex_to_unicode
from xhs_cli.htmlparse import parse_html


class InlineMathTests(unittest.TestCase):
    def test_nested_fraction_has_explicit_bounds(self):
        self.assertEqual(
            latex_to_unicode(r"\frac{1}{1+\frac{x}{y}}"),
            "(1)/(1+(x)/(y))",
        )

    def test_argument_whitespace_is_ignored(self):
        self.assertEqual(
            latex_to_unicode(r"\frac { a + b } { c + d }"),
            "(a + b)/(c + d)",
        )

    def test_complex_scripts_keep_unambiguous_bounds(self):
        self.assertEqual(latex_to_unicode(r"x^{a+\beta}_{i,j}"), "x^(a+β)_(i,j)")

    def test_delimiters_and_escaped_braces(self):
        self.assertEqual(
            latex_to_unicode(r"\left\{\frac{x}{y+1}\right\}"),
            "{(x)/(y+1)}",
        )

    def test_inline_matrix_is_flattened_by_rows_and_columns(self):
        self.assertEqual(
            latex_to_unicode(r"\begin{pmatrix}a&b\\c&d\end{pmatrix}"),
            "(a, b; c, d)",
        )

    def test_inline_cases_are_flattened(self):
        self.assertEqual(
            latex_to_unicode(r"\begin{cases}x&x>0\\-x&x\leq0\end{cases}"),
            "{ x if x>0; -x if x≤0 }",
        )

    def test_greek_relations_and_nabla(self):
        self.assertEqual(
            latex_to_unicode(
                r"\Alpha\Beta\Gamma\alpha\beta\gamma+\nabla f, x\in A, x\neq0"
            ),
            "ΑΒΓαβγ+∇f, x∈A, x≠0",
        )

    def test_eeimg_matrix_remains_in_paragraph_flow(self):
        blocks, _ = parse_html(
            r'<p>令 <img eeimg="1" alt="\begin{pmatrix}a&amp;b\\c&amp;d\end{pmatrix}"> 成立</p>'
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "p")
        self.assertIn("(a, b; c, d)", blocks[0].text.plain)

    def test_display_formula_is_not_duplicated_in_paragraph(self):
        blocks, _ = parse_html(r'<p>前文<img alt="\frac{a}{b}">后文</p>')
        self.assertEqual([block.kind for block in blocks], ["p", "math"])
        self.assertNotIn("a", blocks[0].text.plain)


if __name__ == "__main__":
    unittest.main()
