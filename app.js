const data = [
  {
    id: 1,
    school: "開成中学校",
    schoolType: "男子校",
    subject: "社会",
    year: 2024,
    hensachi: 72,
    hitRate: 91,
    kyogaku: "教学なし",
    snippet: "江戸幕府の成立と徳川家康の政策について説明しなさい。",
    pdfs: [
      "2024_開成_社会_1.pdf",
      "2023_開成_社会_2.pdf",
      "2022_開成_社会_3.pdf",
    ],
  },
  {
    id: 2,
    school: "桜蔭中学校",
    schoolType: "女子校",
    subject: "国語",
    year: 2023,
    hensachi: 71,
    hitRate: 76,
    kyogaku: "教学なし",
    snippet: "徳川家康に関する評論文を読み、筆者の主張をまとめなさい。",
    pdfs: ["2023_桜蔭_国語_1.pdf", "2021_桜蔭_国語_2.pdf"],
  },
  {
    id: 3,
    school: "渋谷教育学園渋谷中学校",
    schoolType: "共学校",
    subject: "理科",
    year: 2025,
    hensachi: 67,
    hitRate: 58,
    kyogaku: "教学あり",
    snippet: "光合成と蒸散の関係を実験結果から考察しなさい。",
    pdfs: ["2025_渋渋_理科_1.pdf"],
  },
  {
    id: 4,
    school: "武蔵中学校",
    schoolType: "男子校",
    subject: "算数",
    year: 2025,
    hensachi: 69,
    hitRate: 64,
    kyogaku: "教学なし",
    snippet: "速さと比を使った旅人算を解きなさい。",
    pdfs: ["2025_武蔵_算数_1.pdf", "2024_武蔵_算数_1.pdf"],
  },
];

const els = {
  keyword: document.querySelector("#keyword"),
  subject: document.querySelector("#subject"),
  schoolType: document.querySelector("#schoolType"),
  hensachiMin: document.querySelector("#hensachiMin"),
  hitRateMin: document.querySelector("#hitRateMin"),
  kyogaku: document.querySelector("#kyogaku"),
  resultCount: document.querySelector("#resultCount"),
  resultList: document.querySelector("#resultList"),
  detail: document.querySelector("#detail"),
  searchBtn: document.querySelector("#searchBtn"),
  clearBtn: document.querySelector("#clearBtn"),
};

function filterData() {
  const keyword = els.keyword.value.trim().toLowerCase();
  const subject = els.subject.value;
  const schoolType = els.schoolType.value;
  const hensachiMin = Number(els.hensachiMin.value || 0);
  const hitRateMin = Number(els.hitRateMin.value || 0);
  const kyogaku = els.kyogaku.value;

  return data.filter((row) => {
    const keywordTarget = `${row.school} ${row.snippet} ${row.subject}`.toLowerCase();

    return (
      (!keyword || keywordTarget.includes(keyword)) &&
      (!subject || row.subject === subject) &&
      (!schoolType || row.schoolType === schoolType) &&
      row.hensachi >= hensachiMin &&
      row.hitRate >= hitRateMin &&
      (!kyogaku || row.kyogaku === kyogaku)
    );
  });
}

function renderResults(rows) {
  els.resultCount.textContent = `${rows.length} 件`;
  els.resultList.innerHTML = "";

  if (rows.length === 0) {
    els.resultList.innerHTML = '<li class="muted">該当する結果はありません。</li>';
    return;
  }

  rows.forEach((row) => {
    const li = document.createElement("li");
    li.className = "result-item";
    li.innerHTML = `
      <strong>${row.year}年 ${row.school}（${row.subject}）</strong>
      <div class="badges">
        <span class="badge">${row.schoolType}</span>
        <span class="badge">偏差値 ${row.hensachi}</span>
        <span class="badge">ヒット率 ${row.hitRate}%</span>
        <span class="badge">${row.kyogaku}</span>
      </div>
      <div>${row.snippet}</div>
    `;
    li.addEventListener("click", () => renderDetail(row));
    els.resultList.appendChild(li);
  });
}

function renderDetail(row) {
  els.detail.innerHTML = `
    <h3>${row.school} / ${row.year}年 / ${row.subject}</h3>
    <p><strong>問題文抜粋:</strong> ${row.snippet}</p>
    <p><strong>該当PDF一覧:</strong></p>
    <ul class="pdf-list">
      ${row.pdfs.map((pdf) => `<li>${pdf}</li>`).join("")}
    </ul>
  `;
}

els.searchBtn.addEventListener("click", () => {
  renderResults(filterData());
});

els.clearBtn.addEventListener("click", () => {
  els.keyword.value = "";
  els.subject.value = "";
  els.schoolType.value = "";
  els.hensachiMin.value = "";
  els.hitRateMin.value = "";
  els.kyogaku.value = "";
  renderResults(data);
  els.detail.textContent = "左の検索結果をクリックすると、ここに該当PDF情報を表示します。";
});

renderResults(data);
