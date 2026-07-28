import unittest

from script import format_search_result
from turbo_database import ArticleMatch, SearchResult


class ResultFormattingTests(unittest.TestCase):
    def test_articles_are_individual_copyable_code_entities(self) -> None:
        result = SearchResult(
            original_query="787556",
            normalized_query="787556",
            matched_query="787556",
            matches=(
                ArticleMatch(
                    article="AL-0045",
                    categories=("Картридж",),
                    matched_types=("Turbo P/N",),
                    sources=("data.csv",),
                ),
                ArticleMatch(
                    article="A&B<2>",
                    categories=("Прочее",),
                    matched_types=("OEM",),
                    sources=("data.csv",),
                ),
            ),
            exact=True,
            truncated=False,
        )

        message = "\n".join(format_search_result(result))

        self.assertIn("<code>AL-0045</code>", message)
        self.assertIn("<code>A&amp;B&lt;2&gt;</code>", message)
        self.assertNotIn("<code>AL-0045, A", message)


if __name__ == "__main__":
    unittest.main()
