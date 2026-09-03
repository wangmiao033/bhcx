'use client';

import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowUpRight,
  Building2,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  CircleCheck,
  Database,
  FileText,
  Gamepad2,
  Info,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  Upload,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { demoGames, demoQueries, type GameRecord } from './game-data';

const STORAGE_KEY = 'banhao-records-v1';
const KNOWN_WORDS = [
  '山海',
  '旅程',
  '星河',
  '小镇',
  '萌宠',
  '乐园',
  '梦幻',
  '冒险',
  '高手',
  '物语',
];

type SearchResult = {
  exact: Array<GameRecord & { aliasMatch: boolean }>;
  similar: Array<GameRecord & { similarity: number }>;
  keywords: KeywordResult[];
};

type KeywordResult = {
  word: string;
  matches: GameRecord[];
  peakYear: string;
  peakCount: number;
  recentCount: number;
  status: '近期可见' | '低频用词' | '疑似消失';
};

type WebMcpContext = {
  registerTool: (
    tool: {
      name: string;
      title: string;
      description: string;
      inputSchema: Record<string, unknown>;
      annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
      execute: (input: unknown) => unknown;
    },
    options?: { signal?: AbortSignal },
  ) => void | Promise<void>;
};

declare global {
  interface Document {
    readonly modelContext?: WebMcpContext;
  }
}

function normalizeName(value: string) {
  return value.replace(/[《》·\s:：—_-]/g, '').toLowerCase();
}

function recordNames(record: GameRecord) {
  return [record.name, ...(record.aliases ?? [])];
}

function bigrams(value: string) {
  const name = normalizeName(value);
  if (name.length < 2) return new Set([name]);
  return new Set(Array.from({ length: name.length - 1 }, (_, index) => name.slice(index, index + 2)));
}

function similarity(a: string, b: string) {
  const setA = bigrams(a);
  const setB = bigrams(b);
  const intersection = [...setA].filter((part) => setB.has(part)).length;
  const dice = (2 * intersection) / Math.max(1, setA.size + setB.size);
  const normalizedA = normalizeName(a);
  const normalizedB = normalizeName(b);
  const containment = normalizedA.includes(normalizedB) || normalizedB.includes(normalizedA) ? 0.2 : 0;
  return Math.min(1, dice + containment);
}

function splitKeywords(query: string, records: GameRecord[]) {
  const cleaned = normalizeName(query);
  const known = KNOWN_WORDS.filter((word) => cleaned.includes(word));
  if (known.length) return [...new Set(known)];

  const candidates: Array<{ word: string; count: number }> = [];
  for (let size = Math.min(4, cleaned.length); size >= 2; size -= 1) {
    for (let index = 0; index <= cleaned.length - size; index += 1) {
      const word = cleaned.slice(index, index + size);
      const count = records.filter((record) => normalizeName(record.name).includes(word)).length;
      if (count) candidates.push({ word, count });
    }
  }

  const selected: string[] = [];
  for (const candidate of candidates.sort((a, b) => b.count - a.count || b.word.length - a.word.length)) {
    if (!selected.some((word) => word.includes(candidate.word) || candidate.word.includes(word))) {
      selected.push(candidate.word);
    }
    if (selected.length === 3) break;
  }
  if (selected.length) return selected;

  const fallback: string[] = [];
  for (let index = 0; index < cleaned.length; index += 2) fallback.push(cleaned.slice(index, index + 2));
  return fallback.filter((word) => word.length > 1);
}

function buildKeyword(word: string, records: GameRecord[], referenceDate: Date): KeywordResult {
  const matches = records
    .filter((record) => recordNames(record).some((name) => normalizeName(name).includes(normalizeName(word))))
    .sort((a, b) => b.approvalDate.localeCompare(a.approvalDate));
  const yearCounts = matches.reduce<Record<string, number>>((counts, record) => {
    const year = record.approvalDate.slice(0, 4);
    counts[year] = (counts[year] ?? 0) + 1;
    return counts;
  }, {});
  const [peakYear = '—', peakCount = 0] = Object.entries(yearCounts).sort(
    (a, b) => b[1] - a[1] || b[0].localeCompare(a[0]),
  )[0] ?? ['—', 0];
  const recentFloor = new Date(referenceDate);
  recentFloor.setFullYear(recentFloor.getFullYear() - 1);
  const recentCount = matches.filter((record) => new Date(record.approvalDate) >= recentFloor).length;
  const status = recentCount > 0 ? '近期可见' : matches.length <= 1 ? '低频用词' : '疑似消失';
  return { word, matches, peakYear, peakCount, recentCount, status };
}

function analyze(query: string, records: GameRecord[]): SearchResult {
  const normalized = normalizeName(query);
  const exact = records
    .filter((record) => recordNames(record).some((name) => normalizeName(name) === normalized))
    .map((record) => ({ ...record, aliasMatch: normalizeName(record.name) !== normalized }));
  const similar = records
    .filter((record) => !recordNames(record).some((name) => normalizeName(name) === normalized))
    .map((record) => ({ ...record, similarity: Math.max(...recordNames(record).map((name) => similarity(query, name))) }))
    .filter((record) => record.similarity >= 0.22)
    .sort((a, b) => b.similarity - a.similarity || b.approvalDate.localeCompare(a.approvalDate))
    .slice(0, 6);
  const newestDate = records.reduce(
    (latest, record) => (record.approvalDate > latest ? record.approvalDate : latest),
    '2024-01-01',
  );
  const referenceDate = new Date(newestDate);
  const keywords = splitKeywords(query, records).map((word) => buildKeyword(word, records, referenceDate));
  return { exact, similar, keywords };
}

function parseCsv(text: string): GameRecord[] {
  const rows = text.trim().split(/\r?\n/).map((line) => line.split(',').map((cell) => cell.trim().replace(/^"|"$/g, '')));
  const headers = rows.shift() ?? [];
  const aliases: Record<keyof GameRecord, string[]> = {
    name: ['name', '游戏名称', '名称'],
    category: ['category', '申报类别', '游戏类别'],
    publisher: ['publisher', '出版单位'],
    operator: ['operator', '运营单位'],
    documentNumber: ['documentNumber', '批复文号', '文号'],
    isbn: ['isbn', '出版物号'],
    approvalDate: ['approvalDate', '批准时间', '审批日期'],
    sourceType: ['sourceType', '来源', '类型'],
  };
  const indexOf = (key: keyof GameRecord) => headers.findIndex((header) => aliases[key].includes(header));
  return rows
    .map((row) => ({
      name: row[indexOf('name')] ?? '',
      category: row[indexOf('category')] ?? '—',
      publisher: row[indexOf('publisher')] ?? '—',
      operator: row[indexOf('operator')] ?? '—',
      documentNumber: row[indexOf('documentNumber')] ?? '—',
      isbn: row[indexOf('isbn')] ?? '—',
      approvalDate: row[indexOf('approvalDate')] ?? '',
      sourceType: (row[indexOf('sourceType')] === '进口' ? '进口' : '国产') as '国产' | '进口',
    }))
    .filter((record) => record.name && /^\d{4}-\d{2}-\d{2}$/.test(record.approvalDate));
}

function normalizeImportedRecord(input: Record<string, unknown>): GameRecord | null {
  const value = (keys: string[]) => keys.map((key) => input[key]).find((item) => typeof item === 'string') as string | undefined;
  const name = value(['name', '游戏名称', '名称']);
  const approvalDate = value(['approvalDate', '批准时间', '审批日期']);
  if (!name || !approvalDate) return null;
  return {
    name,
    aliases: Array.isArray(input.aliases) ? input.aliases.filter((item): item is string => typeof item === 'string') : [],
    category: value(['category', '申报类别', '游戏类别']) ?? '—',
    publisher: value(['publisher', '出版单位']) ?? '—',
    operator: value(['operator', '运营单位']) ?? '—',
    documentNumber: value(['documentNumber', '批复文号', '文号']) ?? '—',
    isbn: value(['isbn', '出版物号']) ?? '—',
    approvalDate,
    sourceType: value(['sourceType', '来源', '类型']) === '进口' ? '进口' : '国产',
    sourceUrl: value(['sourceUrl', '来源链接']),
    changeDate: value(['changeDate', '变更日期']),
    changeInfo: value(['changeInfo', '变更信息']),
  };
}

function RecordCard({ record, score, aliasMatch }: { record: GameRecord; score?: number; aliasMatch?: boolean }) {
  return (
    <article className="rounded-2xl border border-stone-200 bg-white p-5 transition hover:-translate-y-0.5 hover:border-emerald-800/30 hover:shadow-[0_12px_34px_rgb(36_55_44/8%)]">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h4 className="text-lg font-semibold tracking-tight text-stone-950">《{record.name}》</h4>
          <p className="mt-1 text-xs text-stone-500">{record.documentNumber}</p>
        </div>
        {aliasMatch ? (
          <Badge className="bg-amber-100 text-amber-800">历史名称命中</Badge>
        ) : score !== undefined ? (
          <Badge className="bg-emerald-50 text-emerald-800">相似 {Math.round(score * 100)}%</Badge>
        ) : (
          <Badge className="bg-emerald-900 text-white">完全同名</Badge>
        )}
      </div>
      <div className="grid gap-2.5 text-sm text-stone-600">
        <p className="flex gap-2"><CalendarDays className="mt-0.5 size-4 shrink-0 text-stone-400" />{record.approvalDate} · {record.sourceType} · {record.category}</p>
        <p className="flex gap-2"><Building2 className="mt-0.5 size-4 shrink-0 text-stone-400" /><span><b className="font-medium text-stone-800">出版：</b>{record.publisher}</span></p>
        <p className="flex gap-2"><Gamepad2 className="mt-0.5 size-4 shrink-0 text-stone-400" /><span><b className="font-medium text-stone-800">运营：</b>{record.operator}</span></p>
        <p className="flex gap-2"><FileText className="mt-0.5 size-4 shrink-0 text-stone-400" />{record.isbn}</p>
        {!!record.aliases?.length && <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">历史名称：{record.aliases.join('、')}</p>}
        {record.changeInfo && <p className="text-xs leading-5 text-stone-500">最近变更：{record.changeInfo}</p>}
      </div>
    </article>
  );
}

function KeywordCard({ item }: { item: KeywordResult }) {
  const [expanded, setExpanded] = useState(false);
  const isWarning = item.status === '疑似消失';
  const latest = item.matches[0];
  return (
    <article className={`rounded-2xl border p-5 ${isWarning ? 'border-amber-200 bg-amber-50/50' : 'border-stone-200 bg-white'}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="grid size-9 place-items-center rounded-xl bg-stone-950 font-serif text-lg text-white">{item.word.slice(0, 1)}</span>
          <h4 className="text-xl font-semibold tracking-tight">{item.word}</h4>
        </div>
        <Badge className={isWarning ? 'bg-amber-100 text-amber-800' : item.status === '近期可见' ? 'bg-emerald-50 text-emerald-800' : 'bg-stone-100 text-stone-600'}>
          {isWarning ? <TriangleAlert /> : <CircleCheck />}{item.status}
        </Badge>
      </div>

      <div className="mt-5 grid grid-cols-3 divide-x divide-stone-200 rounded-xl bg-stone-50 py-3 text-center">
        <div><b className="block text-lg text-stone-950">{item.matches.length}</b><span className="text-xs text-stone-500">历史记录</span></div>
        <div><b className="block text-lg text-stone-950">{item.peakYear}</b><span className="text-xs text-stone-500">峰值年份 · {item.peakCount} 款</span></div>
        <div><b className="block text-lg text-stone-950">{item.recentCount}</b><span className="text-xs text-stone-500">近一年</span></div>
      </div>

      <p className="mt-4 text-sm leading-6 text-stone-500">
        相关名称：{item.matches.slice(0, 4).map((record) => record.name).join('、') || '当前数据中暂无相关记录'}
      </p>
      {latest && (
        <div className="mt-4 rounded-xl border border-stone-200/80 bg-white/80 p-3.5 text-sm">
          <p className="mb-1 text-xs font-medium text-stone-400">最近一条</p>
          <p className="font-medium text-stone-800">{latest.approvalDate} · 《{latest.name}》</p>
          <p className="mt-1 truncate text-xs text-stone-500">{latest.publisher}</p>
        </div>
      )}
      {item.matches.length > 1 && (
        <>
          <Button variant="ghost" className="mt-3 w-full text-emerald-900" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起记录' : `查看全部 ${item.matches.length} 条`}
            {expanded ? <ChevronUp /> : <ChevronDown />}
          </Button>
          {expanded && (
            <div className="mt-2 divide-y divide-stone-100 border-t border-stone-100">
              {item.matches.map((record) => (
                <div key={`${record.documentNumber}-${record.name}`} className="flex items-center justify-between gap-4 py-2.5 text-xs">
                  <span className="font-medium text-stone-700">《{record.name}》</span>
                  <span className="shrink-0 text-stone-400">{record.approvalDate}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </article>
  );
}

export default function Home() {
  const [records, setRecords] = useState<GameRecord[]>(demoGames);
  const [dataSource, setDataSource] = useState<'loading' | 'official' | 'local' | 'demo'>('loading');
  const [input, setInput] = useState('山海旅程');
  const [query, setQuery] = useState('山海旅程');
  const [notice, setNotice] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const result = useMemo(() => analyze(query, records), [query, records]);
  const latestDate = useMemo(() => records.reduce((latest, record) => record.approvalDate > latest ? record.approvalDate : latest, '—'), [records]);
  const uniqueNames = useMemo(() => new Set(records.map((record) => normalizeName(record.name))).size, [records]);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved) {
        setRecords(JSON.parse(saved) as GameRecord[]);
        setDataSource('local');
        return;
      }
    } catch {
      window.localStorage.removeItem(STORAGE_KEY);
    }
    fetch('/nppa-games.json')
      .then((response) => {
        if (!response.ok) throw new Error('missing');
        return response.json() as Promise<{ records?: GameRecord[] }>;
      })
      .then((payload) => {
        if (!payload.records?.length) throw new Error('empty');
        setRecords(payload.records);
        setDataSource('official');
      })
      .catch(() => setDataSource('demo'));
  }, []);

  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(
      context.registerTool(
        {
          name: 'search_game_name',
          title: '查询游戏名称',
          description: '在当前版号资料库中查询一个游戏名称，并更新页面上的同名、近似名称和关键词分析结果。',
          inputSchema: {
            type: 'object',
            properties: {
              name: { type: 'string', minLength: 2, maxLength: 30, description: '需要查询的中文游戏名称' },
            },
            required: ['name'],
            additionalProperties: false,
          },
          annotations: { readOnlyHint: false, untrustedContentHint: false },
          execute(inputValue) {
            const candidate = inputValue as { name?: unknown };
            if (typeof candidate?.name !== 'string') throw new Error('name 必须是字符串');
            const name = candidate.name.trim();
            if (name.length < 2 || name.length > 30) throw new Error('name 长度必须为 2—30 个字符');
            const nextResult = analyze(name, records);
            setInput(name);
            setQuery(name);
            setNotice('');
            return {
              name,
              exactCount: nextResult.exact.length,
              similarCount: nextResult.similar.length,
              keywords: nextResult.keywords.map((item) => ({ word: item.word, status: item.status, count: item.matches.length })),
            };
          },
        },
        { signal: lifecycle.signal },
      ),
    ).catch(() => undefined);
    return () => lifecycle.abort();
  }, [records]);

  function onSearch(event: FormEvent) {
    event.preventDefault();
    const value = input.trim();
    if (value.length < 2 || value.length > 30) {
      setNotice('请输入 2—30 个字符的游戏名称');
      return;
    }
    setNotice('');
    setQuery(value);
  }

  function runExample(value: string) {
    setInput(value);
    setQuery(value);
    setNotice('');
  }

  async function importFile(file?: File) {
    if (!file) return;
    try {
      const text = await file.text();
      const jsonValue = file.name.toLowerCase().endsWith('.csv') ? null : JSON.parse(text) as unknown;
      const jsonRows = Array.isArray(jsonValue)
        ? jsonValue
        : ((jsonValue as { records?: unknown[] } | null)?.records ?? []);
      const parsed = file.name.toLowerCase().endsWith('.csv')
        ? parseCsv(text)
        : jsonRows.map((item) => normalizeImportedRecord(item as Record<string, unknown>)).filter(Boolean) as GameRecord[];
      if (!parsed.length) throw new Error('empty');
      const deduped = [...new Map(parsed.map((record) => [`${record.documentNumber}-${normalizeName(record.name)}`, record])).values()];
      setRecords(deduped);
      setDataSource('local');
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(deduped));
      setNotice(`已导入 ${deduped.length.toLocaleString('zh-CN')} 条记录，数据仅保存在当前浏览器。`);
    } catch {
      setNotice('导入失败：请检查 JSON / CSV 字段和日期格式。');
    } finally {
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  async function resetData() {
    window.localStorage.removeItem(STORAGE_KEY);
    try {
      const response = await fetch('/nppa-games.json');
      const payload = await response.json() as { records?: GameRecord[] };
      if (!payload.records?.length) throw new Error('empty');
      setRecords(payload.records);
      setDataSource('official');
      setNotice('已恢复内置的官方公示数据。');
    } catch {
      setRecords(demoGames);
      setDataSource('demo');
      setNotice('未找到公示数据文件，已恢复演示数据。');
    }
  }

  return (
    <main className="min-h-screen bg-[#f6f3ed] text-stone-900">
      <header className="border-b border-stone-200/90 bg-[#fbfaf7]/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <div className="grid size-10 place-items-center rounded-xl bg-emerald-950 text-white shadow-sm"><ShieldCheck className="size-5" /></div>
            <div>
              <p className="font-serif text-lg font-semibold tracking-[0.08em]">版号名称查询</p>
              <p className="text-[11px] text-stone-500">个人名称检索与关键词分析</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <input ref={fileRef} type="file" accept=".json,.csv" className="hidden" onChange={(event) => importFile(event.target.files?.[0])} />
            <Button variant="outline" className="h-9 rounded-xl border-stone-300 bg-white" onClick={() => fileRef.current?.click()}>
              <Upload /> <span className="hidden sm:inline">导入数据</span>
            </Button>
            <Button variant="ghost" size="icon-lg" aria-label="恢复演示数据" title="恢复演示数据" onClick={resetData}><RotateCcw /></Button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-7xl px-4 py-7 sm:px-6 lg:px-8 lg:py-10">
        <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_310px]">
          <Card className="relative overflow-hidden rounded-[28px] border-0 bg-emerald-950 text-white shadow-[0_22px_60px_rgb(23_58_46/16%)] ring-0">
            <div className="absolute right-0 top-0 h-48 w-48 translate-x-16 -translate-y-20 rounded-full border-[28px] border-white/5" />
            <CardHeader className="relative px-6 pt-6 sm:px-8 sm:pt-8">
              <div className="mb-4 flex items-center gap-2 text-xs font-medium text-emerald-200"><Sparkles className="size-4" /> 名称碰撞一查即知</div>
              <h1 className="max-w-2xl font-serif text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">查询游戏名是否已被使用</h1>
              <p className="mt-2 max-w-xl text-sm leading-6 text-emerald-100/70">同时检查完全同名、近似名称与关键词历史趋势，辅助新名称初筛。</p>
            </CardHeader>
            <CardContent className="relative px-6 pb-6 sm:px-8 sm:pb-8">
              <form onSubmit={onSearch} className="mt-2 flex flex-col gap-3 rounded-2xl bg-white p-2 shadow-xl sm:flex-row">
                <div className="relative flex-1">
                  <Search className="pointer-events-none absolute left-3.5 top-1/2 size-5 -translate-y-1/2 text-stone-400" />
                  <Input
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                    placeholder="输入拟用游戏名称，如：山海旅程"
                    aria-label="游戏名称"
                    className="h-12 border-0 bg-transparent pl-11 text-base text-stone-950 shadow-none focus-visible:ring-0"
                  />
                </div>
                <Button type="submit" className="h-12 rounded-xl bg-[#b8513b] px-7 text-base text-white hover:bg-[#a74632]">开始查询 <ArrowUpRight /></Button>
              </form>
              <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-emerald-100/60">
                <span>试试：</span>
                {demoQueries.map((item) => <button key={item} onClick={() => runExample(item)} className="rounded-full border border-white/15 px-3 py-1.5 text-emerald-50 transition hover:border-white/30 hover:bg-white/10">{item}</button>)}
              </div>
              {notice && <p className="mt-3 text-sm text-amber-200" role="status">{notice}</p>}
            </CardContent>
          </Card>

          <Card className="rounded-[28px] border-stone-200 bg-[#fbfaf7] py-0 shadow-none">
            <CardContent className="flex h-full flex-col justify-between p-6">
              <div>
                <div className="mb-6 flex items-center justify-between"><span className="text-xs font-medium uppercase tracking-[0.14em] text-stone-400">当前资料库</span><Database className="size-5 text-emerald-900" /></div>
                <p className="font-serif text-4xl font-semibold tracking-tight">{records.length.toLocaleString('zh-CN')}</p>
                <p className="mt-1 text-sm text-stone-500">条版号记录 · {uniqueNames.toLocaleString('zh-CN')} 个名称</p>
              </div>
              <div className="mt-8 border-t border-stone-200 pt-4 text-xs leading-6 text-stone-500">
                <p className="flex items-center justify-between"><span>最新批准时间</span><b className="font-medium text-stone-700">{latestDate}</b></p>
                <p className="mt-1 flex items-center justify-between"><span>数据状态</span><span className={`flex items-center gap-1 ${dataSource === 'official' ? 'text-emerald-700' : 'text-amber-700'}`}><span className={`size-1.5 rounded-full ${dataSource === 'official' ? 'bg-emerald-500' : 'bg-amber-500'}`} />{dataSource === 'loading' ? '正在载入' : dataSource === 'official' ? '官方公示数据' : dataSource === 'local' ? '本地导入数据' : '演示数据'}</span></p>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="mt-8 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_330px]">
          <div className="space-y-6">
            <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
              <div>
                <p className="text-xs font-medium uppercase tracking-[0.16em] text-emerald-800">查询结果</p>
                <h2 className="mt-1 font-serif text-3xl font-semibold">《{query}》</h2>
              </div>
              <div className="flex gap-2 text-sm">
                <Badge variant="outline" className="h-7 border-stone-300 bg-white px-3 text-stone-600">同名 {result.exact.length}</Badge>
                <Badge variant="outline" className="h-7 border-stone-300 bg-white px-3 text-stone-600">近似 {result.similar.length}</Badge>
              </div>
            </div>

            <Card className="rounded-2xl border-stone-200 bg-[#fbfaf7] shadow-none">
              <CardHeader className="border-b border-stone-200/80 px-5 pb-4">
                <div className="flex items-center justify-between"><h3 className="text-base font-semibold">同名与近似名称</h3><span className="text-xs text-stone-400">按相似度排序</span></div>
              </CardHeader>
              <CardContent className="p-5">
                {result.exact.length === 0 && (
                  <div className="mb-4 flex items-start gap-3 rounded-xl border border-emerald-100 bg-emerald-50/70 p-4 text-sm text-emerald-900">
                    <CircleCheck className="mt-0.5 size-5 shrink-0" /><div><b className="font-semibold">当前资料库未发现完全同名</b><p className="mt-1 text-xs leading-5 text-emerald-800/70">这不代表名称一定可用，仍需结合商标、著作权及最新官方公示判断。</p></div>
                  </div>
                )}
                <div className="grid gap-4 xl:grid-cols-2">
                  {result.exact.map((record) => <RecordCard key={`${record.documentNumber}-${record.name}`} record={record} aliasMatch={record.aliasMatch} />)}
                  {result.similar.map((record) => <RecordCard key={`${record.documentNumber}-${record.name}`} record={record} score={record.similarity} />)}
                </div>
                {!result.exact.length && !result.similar.length && (
                  <div className="py-12 text-center"><Search className="mx-auto size-8 text-stone-300" /><p className="mt-3 font-medium text-stone-600">没有发现近似名称</p><p className="mt-1 text-sm text-stone-400">可导入更完整的版号数据后再次查询</p></div>
                )}
              </CardContent>
            </Card>
          </div>

          <aside className="space-y-4 lg:sticky lg:top-5">
            <div className="flex items-end justify-between"><div><p className="text-xs font-medium uppercase tracking-[0.16em] text-emerald-800">用词观察</p><h3 className="mt-1 font-serif text-2xl font-semibold">关键词拆解</h3></div><span className="text-xs text-stone-400">{result.keywords.length} 个词</span></div>
            {result.keywords.map((item) => <KeywordCard key={item.word} item={item} />)}
            <div className="rounded-2xl border border-stone-200 bg-stone-100/70 p-4 text-xs leading-5 text-stone-500">
              <p className="mb-1 flex items-center gap-2 font-medium text-stone-700"><Info className="size-4" />趋势标签说明</p>
              “近期可见”指数据最新日期前一年内仍有记录；“疑似消失”仅描述词频变化，不构成审批判断。
            </div>
          </aside>
        </section>

        <footer className="mt-12 border-t border-stone-200 py-7 text-xs leading-6 text-stone-500">
          <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
            <div><p className="font-medium text-stone-700">数据说明与免责声明</p><p className="mt-1 max-w-3xl">内置数据来自国家新闻出版署公开的网络游戏审批信息，并合并可匹配的审批变更记录；查询结果仅供名称初筛与个人研究，不构成专业意见、审批预测或权利判断。</p></div>
            <a href="https://www.nppa.gov.cn/bsfw/jggs/yxspjg/gcwlyxspxx/index.html" target="_blank" rel="noreferrer" className="flex shrink-0 items-center gap-1.5 font-medium text-emerald-900 hover:underline">国家新闻出版署 <ArrowUpRight className="size-3.5" /></a>
          </div>
        </footer>
      </div>
    </main>
  );
}
