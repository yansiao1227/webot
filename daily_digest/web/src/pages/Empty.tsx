import { Header, Footer } from '../components/PageLayout';

export function Empty({ error = false, missing = false }: { error?: boolean; missing?: boolean }) {
  return (
    <>
      <Header />
      <main className="empty">
        <div className="kicker">THE DAILY OBSERVER</div>
        <h1>
          {missing
            ? '这一页，暂未收录。'
            : error
              ? '日报暂时无法读取。'
              : '今天的观察，正在准备中。'}
        </h1>
        <p>
          {missing
            ? '可以回到头版，或翻阅往期日报。'
            : error
              ? '请稍后重试，已有内容不会因此重新发送。'
              : '每日整理 AI、社会热点与两地收盘，正式日报生成后在这里阅读。'}
        </p>
        <a className="outline-link" href="/">
          返回头版
        </a>
        <a className="read-link" href="/preview">
          查看排版样本
        </a>
      </main>
      <Footer />
    </>
  );
}
