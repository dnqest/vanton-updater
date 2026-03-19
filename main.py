# main.py — автоматическое обновление отчётов
# 1) скачиваем CSV с FTP (с retry)
# 2) генерируем QuantStats HTML (с quantiles)
# 3) применяем CSS-фиксы
# 4) перерисовываем SVG "Cumulative Returns" (проценты + запас по Y)

import os
import ftplib
import io
import re
import time

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import quantstats_lumi as qs

plt.rcParams['axes.ymargin'] = 0.10


class AutomatedFTPUpdater:
    def __init__(self):
        # FTP с CSV отчётами — значения из переменных окружения
        self.ftp_config = {
            'host': os.environ['PNL_FTP_HOST'],
            'port': int(os.environ.get('PNL_FTP_PORT', '21')),
            'user': os.environ['PNL_FTP_USER'],
            'passwd': os.environ['PNL_FTP_PASS'],
            'timeout': int(os.environ.get('PNL_FTP_TIMEOUT', '120')),
        }

        self.accounts = {
            "ICE_LAB_ICM": {"name": "Prime", "filename": "prime.html"},
            "44243855": {"name": "Cross-Market", "filename": "crossmarket.html"},
            "237585858": {"name": "High-Risk", "filename": "highrisk.html"},
        }

        self.files = [f"{code}_PnL.csv" for code in self.accounts.keys()]

    # 1) скачать CSV с RETRY
    def download_files(self, max_retries=3, retry_delay=5):
        """Скачивание файлов с FTP с автоматическими повторами при сбоях"""

        for attempt in range(max_retries):
            try:
                print(f"FTP connect... (попытка {attempt + 1}/{max_retries})")
                ftp = ftplib.FTP()
                ftp.connect(
                    self.ftp_config['host'],
                    self.ftp_config['port'],
                    timeout=self.ftp_config['timeout'],
                )

                print("login...")
                ftp.login(self.ftp_config['user'], self.ftp_config['passwd'])
                ftp.set_pasv(True)
                print("PWD:", ftp.pwd())

                downloaded = []
                for filename in self.files:
                    try:
                        with open(filename, 'wb') as f:
                            ftp.retrbinary(f"RETR {filename}", f.write)
                        print(f"✅ CSV: {filename}")
                        downloaded.append(filename)
                    except Exception as file_error:
                        print(f"⚠️ Ошибка скачивания {filename}: {file_error}")
                        continue

                try:
                    ftp.quit()
                except Exception:
                    ftp.close()

                if downloaded:
                    print(
                        f"✅ CSV скачано {len(downloaded)}/{len(self.files)}: "
                        + ", ".join(downloaded)
                        + "\n"
                    )
                    return True
                else:
                    raise Exception("Не удалось скачать ни одного файла")

            except Exception as e:
                print(f"❌ FTP error (попытка {attempt + 1}/{max_retries}): {e}")

                if attempt < max_retries - 1:
                    print(f"Пауза {retry_delay} сек перед повтором...")
                    time.sleep(retry_delay)
                else:
                    print("❌ Превышено число попыток подключения к FTP")
                    return False

        return False

    #подготовка данных
    def _prepare_returns(self, code: str):
        csv_file = f"{code}_PnL.csv"

        df = pd.read_csv(csv_file)
        df.columns = [c.replace('*', '') for c in df.columns]

        if 'date' not in df.columns or 'daily_return' not in df.columns:
            raise ValueError(
                f"Ожидаю колонки 'date' и 'daily_return' в {csv_file}, имею: {list(df.columns)}"
            )

        df = df[df['date'].astype(str).str.match(
            r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}"
        )]

        df['date'] = pd.to_datetime(
            df['date'].str.strip(),
            format='%Y.%m.%d %H:%M',
        )

        df = df.set_index('date')
        returns = df['daily_return'] / 100.0

        return returns

    #2) QuantStats HTML
    def _build_report(self, returns, out_html, title):
        qs.reports.html(
            returns,
            output=out_html,
            title=title,
            metrics=['returns', 'quantiles'],
        )
        print(f"✅ Отчёт создан: {out_html}")

    #3) CSS-фикс
    def _postprocess_styles(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()

            html = re.sub(
                r"overflow:\s*hidden\s*;",
                "overflow: visible;",
                html,
                flags=re.I,
            )

            anti_clip = """
<style id="anti-clip">
  .container, #left, #monthly_heatmap { overflow: visible !important; }
  #left { margin-top: 0 !important; padding-top: 8px !important; }
  #left svg, #monthly_heatmap svg {
      margin: 0 !important;
      padding: 14px 0 !important;
  }
</style>
"""
            html = re.sub(r"</head>", anti_clip + "\n</head>", html, flags=re.I)

            with open(path, "w", encoding="utf-8") as f:
                f.write(html)

            print(f"✅ CSS-фикс применён: {path}")

        except Exception as e:
            print(f"⚠️ CSS-фикс не применён для {path}: {e}")

    #4) Замена SVG Cumulative Returns
    def _replace_cumreturns_svg(self, returns, html_path: str, headroom: float = 0.12):
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()

            m_title = re.search(r"Cumulative\s+Returns", html, flags=re.I)
            if not m_title:
                print(f"⚠️ Cumulative Returns не найден в {html_path}")
                return

            title_pos = m_title.start()
            svg_start = html.rfind("<svg", 0, title_pos)
            svg_end = html.find("</svg>", title_pos)

            if svg_start == -1 or svg_end == -1:
                print(f"⚠️ SVG не найден в {html_path}")
                return

            svg_end += len("</svg>")
            original_svg = html[svg_start:svg_end]

            width_pt, height_pt = 576.0, 432.0
            mw = re.search(r'width="([0-9.]+)pt"', original_svg)
            mh = re.search(r'height="([0-9.]+)pt"', original_svg)
            if mw:
                width_pt = float(mw.group(1))
            if mh:
                height_pt = float(mh.group(1))

            cum = (1.0 + returns).cumprod() - 1.0

            fig, ax = plt.subplots(
                figsize=(width_pt / 72.0, height_pt / 72.0),
            )

            ax.plot(cum.index, cum.values)
            ax.set_title("Cumulative Returns")
            ax.grid(True, alpha=0.3)
            ax.yaxis.set_major_formatter(
                PercentFormatter(xmax=1.0, decimals=0),
            )

            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

            ymin, ymax = ax.get_ylim()
            ax.set_ylim(ymin, ymax * (1.0 + headroom))

            buf = io.StringIO()
            fig.tight_layout()
            fig.savefig(buf, format="svg")
            plt.close(fig)

            new_svg = buf.getvalue()
            new_svg = re.sub(r'width="[^"]+"', f'width="{width_pt}pt"', new_svg)
            new_svg = re.sub(r'height="[^"]+"', f'height="{height_pt}pt"', new_svg)

            html = html[:svg_start] + new_svg + html[svg_end:]

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            print(f"✅ SVG Cumulative Returns обновлён: {html_path}")

        except Exception as e:
            print(f"⚠️ SVG ошибка в {html_path}: {e}")

    #полный цикл
    def generate_reports(self):
        try:
            for code, info in self.accounts.items():
                csv_file = f"{code}_PnL.csv"

                if not os.path.exists(csv_file):
                    print(f"⚠️ Файл {csv_file} не найден, пропускаю {info['name']}")
                    continue

                returns = self._prepare_returns(code)
                out_html = info['filename']

                self._build_report(
                    returns,
                    out_html,
                    f"{info['name']} Strategy Performance",
                )

                self._postprocess_styles(out_html)
                self._replace_cumreturns_svg(returns, out_html)

            print("\n✅ Все отчёты готовы и исправлены.\n")
            return True

        except Exception as e:
            print(f"❌ Ошибка генерации: {e}")
            return False


if __name__ == "__main__":
    updater = AutomatedFTPUpdater()
    if updater.download_files():
        updater.generate_reports()
    else:
        print("⚠️ Не удалось скачать файлы с FTP, но продолжу генерацию из существующих")
        updater.generate_reports()
