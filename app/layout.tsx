import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '版号名称查询｜游戏名称重名检索',
  description: '面向个人使用的网络游戏版号名称检索、近似名称匹配与关键词趋势分析工具。',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
