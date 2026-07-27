import unittest
from email.message import Message

from vin_emex_catalog import EmexCatalogError, EmexVinCatalog
from vin_search import VinRecord


VIN = "WAUZZZF40KA080440"


class FakeResponse:
    def __init__(self, url: str, html: str, *, final_url: str | None = None):
        self.url = final_url or url
        self.payload = html.encode("utf-8")
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


class FakeOpener:
    def __init__(self, pages: dict[str, str], *, redirect_host: str = ""):
        self.pages = pages
        self.redirect_host = redirect_host
        self.requested: list[str] = []

    def open(self, request, *, timeout):
        url = request.full_url
        self.requested.append(url)
        if self.redirect_host:
            return FakeResponse(
                url,
                "",
                final_url=f"https://{self.redirect_host}/Vehicle.aspx",
            )
        for marker, html in self.pages.items():
            if marker in url:
                return FakeResponse(url, html)
        raise AssertionError(f"Unexpected URL: {url}")


def catalog_pages() -> dict[str, str]:
    return {
        "Vehicles.aspx": f"""
            <html><body>
              <a href="https://untrusted.example/Vehicle.aspx?vin={VIN}">
                Bad result
              </a>
              <a href="Vehicle.aspx?c=AU1587&amp;vid=0&amp;vin={VIN}">
                A4/Avant
              </a>
            </body></html>
        """,
        "Vehicle.aspx": f"""
            <html><body>
              <h1>Автомобиль Audi - A4/Avant</h1>
              <a href="Unit.aspx?c=AU1587&amp;uid=1&amp;vin={VIN}">
                <b>145-020:</b> Турбонагнетатель
              </a>
              <a href="Unit.aspx?c=AU1587&amp;uid=2&amp;vin={VIN}">
                <b>100-010:</b> Двигатель
              </a>
            </body></html>
        """,
        "Unit.aspx": f"""
            <html><body><table>
              <tr>
                <td name="c_pnc">
                  <a href="/Search.aspx?n=06L145874E&amp;catalog=Audi&amp;vin={VIN}&amp;source=units">1</a>
                </td>
                <td name="c_oem">06L145874E</td>
                <td name="c_name">Турбонагнетатель</td>
              </tr>
              <tr>
                <td name="c_pnc">
                  <a href="/Search.aspx?n=06L145722TX&amp;catalog=Audi&amp;vin={VIN}&amp;source=units">1</a>
                </td>
                <td name="c_oem">06L145722TX</td>
                <td name="c_name">Турбонагнетатель</td>
              </tr>
              <tr>
                <td name="c_pnc">
                  <a href="/Search.aspx?n=06L145778H&amp;catalog=Audi&amp;vin={VIN}&amp;source=units">2</a>
                </td>
                <td name="c_oem">06L145778H</td>
                <td name="c_name">Масляная трубка турбонагнетателя</td>
              </tr>
            </table></body></html>
        """,
    }


class EmexVinCatalogTests(unittest.TestCase):
    def test_follows_vin_vehicle_and_turbo_group(self) -> None:
        opener = FakeOpener(catalog_pages())

        report = EmexVinCatalog(opener=opener).search(
            VinRecord(vin=VIN, status="pending", model_year="2019")
        )

        self.assertEqual(report.status, "found")
        self.assertEqual(report.record.make, "Audi")
        self.assertEqual(report.record.model, "A4/Avant")
        self.assertEqual(report.record.model_year, "2019")
        self.assertEqual(
            report.record.fitments[0].oem_numbers,
            ("06L145874E", "06L145722TX"),
        )
        self.assertNotIn(
            "06L145778H",
            report.record.fitments[0].oem_numbers,
        )
        self.assertEqual(len(report.record.sources), 2)
        self.assertEqual(len(opener.requested), 3)

    def test_vehicle_without_turbo_group_is_not_success(self) -> None:
        pages = catalog_pages()
        pages["Vehicle.aspx"] = (
            f'<h1>Автомобиль Audi - A4/Avant</h1>'
            f'<a href="Unit.aspx?uid=2&amp;vin={VIN}">'
            "<b>100-010:</b> Двигатель</a>"
        )

        report = EmexVinCatalog(opener=FakeOpener(pages)).search(
            VinRecord(vin=VIN, status="pending")
        )

        self.assertEqual(report.status, "not_found")
        self.assertEqual(report.record.fitments, ())

    def test_rejects_redirect_outside_emex(self) -> None:
        with self.assertRaises(EmexCatalogError):
            EmexVinCatalog(
                opener=FakeOpener({}, redirect_host="untrusted.example")
            ).search(VinRecord(vin=VIN, status="pending"))


if __name__ == "__main__":
    unittest.main()
