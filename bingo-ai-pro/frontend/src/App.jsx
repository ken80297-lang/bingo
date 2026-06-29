import { useEffect, useState } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

function StatCard({ title, value, label }) {
  return (
    <div className="rounded-3xl border border-slate-800/80 bg-slate-900/80 p-4 shadow-lg shadow-slate-950/20 backdrop-blur">
      <p className="text-sm uppercase tracking-[0.25em] text-slate-400">{title}</p>
      <p className="mt-3 text-3xl font-semibold text-slate-50">{value}</p>
      {label ? <p className="mt-1 text-sm text-slate-400">{label}</p> : null}
    </div>
  );
}

function Badge({ value }) {
  return (
    <span className="inline-flex rounded-full bg-emerald-500/15 px-3 py-1 text-sm font-medium text-emerald-300 ring-1 ring-emerald-400/20">{value}</span>
  );
}

function Panel({ title, items }) {
  return (
    <section className="space-y-3 rounded-3xl border border-slate-800/80 bg-slate-900/80 p-6 shadow-lg shadow-slate-950/10">
      <h2 className="text-xl font-semibold text-slate-100">{title}</h2>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {items.map((item) => (
          <div key={item.label} className="rounded-2xl bg-slate-950/70 p-4">
            <p className="text-sm text-slate-400">{item.label}</p>
            <p className="mt-2 text-lg font-semibold text-slate-100">{item.value}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function SectionCard({ title, children }) {
  return (
    <div className="rounded-3xl border border-slate-800/80 bg-slate-900/80 p-5 shadow-lg shadow-slate-950/10">
      <h3 className="text-lg font-semibold text-slate-100">{title}</h3>
      <div className="mt-4 space-y-3">{children}</div>
    </div>
  );
}

function formatNumbers(numbers = []) {
  return numbers.map((n) => <Badge key={n} value={n} />);
}

function App() {
  const [data, setData] = useState(null);
  const [darkMode, setDarkMode] = useState(true);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
  }, [darkMode]);

  useEffect(() => {
    fetch(`${API_BASE}/latest`)
      .then((res) => res.json())
      .then((payload) => setData(payload))
      .catch(() => setData(null));
  }, []);

  const refresh = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/update`, { method: 'POST' });
      const payload = await res.json();
      setData(payload);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-950 via-slate-900 to-slate-950 text-slate-100">
      <div className="mx-auto flex min-h-screen max-w-7xl flex-col gap-8 px-4 py-8 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-4 rounded-3xl border border-slate-800/90 bg-slate-950/80 p-5 shadow-xl shadow-slate-950/20 backdrop-blur md:flex-row md:items-center md:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.3em] text-emerald-300/80">Bingo AI Pro</p>
            <h1 className="mt-3 text-3xl font-semibold text-slate-50">Bingo 分析與推薦</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-400">最新開獎、趨勢分析、今日推薦號碼與統計。支援 PWA 及深色模式。</p>
          </div>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <button onClick={refresh} className="rounded-2xl bg-emerald-500 px-5 py-3 text-sm font-semibold text-slate-950 transition hover:bg-emerald-400" disabled={loading}>
              {loading ? '更新中...' : '立即更新'}
            </button>
            <button onClick={() => setDarkMode((prev) => !prev)} className="rounded-2xl border border-slate-700/80 bg-slate-900 px-5 py-3 text-sm font-semibold text-slate-100 transition hover:border-slate-500">
              {darkMode ? '淺色模式' : '深色模式'}
            </button>
          </div>
        </header>

        <main className="grid gap-6 lg:grid-cols-[1.5fr_1fr]">
          <div className="space-y-6">
            <SectionCard title="最新一期 Bingo Bingo">
              <div className="grid gap-4 sm:grid-cols-2">
                <StatCard title="期別" value={data?.latest?.issue ?? '---'} />
                <StatCard title="開獎時間" value={data?.latest?.time_text ?? '---'} />
              </div>
              <div>
                <p className="text-sm text-slate-400">最新開獎號碼</p>
                <div className="mt-3 flex flex-wrap gap-2">{formatNumbers(data?.latest?.numbers ?? [])}</div>
              </div>
            </SectionCard>

            <SectionCard title="今日推薦">
              <div className="flex flex-wrap gap-2">{formatNumbers(data?.recommendation?.recommendation_numbers ?? [])}</div>
            </SectionCard>

            <Panel
              title="分析摘要"
              items={[
                { label: '信心指數', value: data?.statistics?.latest_issue ? '高' : '---' },
                { label: '熱號', value: (data?.analysis?.hot?.hot_numbers || []).slice(0, 5).join(' ') || '---' },
                { label: '冷號', value: (data?.analysis?.cold?.cold_numbers || []).slice(0, 5).join(' ') || '---' },
                { label: '補號', value: (data?.analysis?.missing?.missing_numbers || []).slice(0, 5).join(' ') || '---' },
                { label: '重號', value: (data?.analysis?.repeat?.repeat_numbers || []).join(' ') || '---' },
                { label: '雙生號', value: (data?.analysis?.pair?.pairs || []).map((pair) => pair.join('')).join(' ') || '---' },
                { label: '大小比例', value: data?.analysis?.size?.ratio ?? '---' },
                { label: '單雙比例', value: data?.analysis?.odd_even?.ratio ?? '---' },
              ]}
            />
          </div>

          <div className="space-y-6">
            <SectionCard title="歷史分析面板">
              <div className="grid gap-4">
                <div className="rounded-3xl bg-slate-950/80 p-4">
                  <p className="text-sm text-slate-400">熱號</p>
                  <div className="mt-3 flex flex-wrap gap-2">{formatNumbers(data?.analysis?.hot?.hot_numbers ?? [])}</div>
                </div>
                <div className="rounded-3xl bg-slate-950/80 p-4">
                  <p className="text-sm text-slate-400">冷號</p>
                  <div className="mt-3 flex flex-wrap gap-2">{formatNumbers(data?.analysis?.cold?.cold_numbers ?? [])}</div>
                </div>
                <div className="rounded-3xl bg-slate-950/80 p-4">
                  <p className="text-sm text-slate-400">超級獎號</p>
                  <div className="mt-3 flex flex-wrap gap-2">{formatNumbers(data?.analysis?.super?.super_candidates ?? [])}</div>
                </div>
                <div className="rounded-3xl bg-slate-950/80 p-4">
                  <p className="text-sm text-slate-400">斜線候選</p>
                  <div className="mt-3 flex flex-wrap gap-2">{formatNumbers(data?.analysis?.diagonal?.diagonal_candidates ?? [])}</div>
                </div>
              </div>
            </SectionCard>
          </div>
        </main>

        <footer className="rounded-3xl border border-slate-800/80 bg-slate-900/80 p-5 text-sm text-slate-500">
          <p>最後更新: {data?.state?.last_update ?? '---'}</p>
          <p className="mt-2">本網站為統計分析用途，僅供參考。</p>
        </footer>
      </div>
    </div>
  );
}

export default App;
