# 日报配图来源

本目录图片用于日报本地预览，元数据见 `news-media.json`。新闻配图使用来源页面实际提供的图片；来源原图中的示范画面和可视化不代表新闻现场照片。

| 文件             | 来源页面                                                                                    | 图片原址                                                                                                 | 说明                                                                          |
| ---------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| news-gemini.webp | https://deepmind.google/blog/gemini-omni-12-flash-fast-and-cost-effective-with-longer-clips | https://storage.googleapis.com/gweb-uniblog-publish-prod/images/gemini-omni-1.2_FZLql41.width-1300.png   | Google DeepMind 官方产品发布封面，仅转码为 WebP，保留尺寸与构图               |
| news-cosmos.webp | https://huggingface.co/blog/nvidia/cosmos-interactive-world-simulation                      | https://cdn-uploads.huggingface.co/production/uploads/677c471f8b4c4e271e57eaa5/rtmGVvFdD6JqkjBc26vsp.png | NVIDIA / Hugging Face 官方文章的点云可视化封面，仅转码为 WebP，保留尺寸与构图 |

以上新闻图片的公开转载授权尚未核实，当前仅用于本地预览；公开发布前应另行核实使用权限。图片归各自权利人所有。

北京生态规划新闻正文没有适用原图，因此可使用明确标识的 AI 主题插画；插画不呈现真实项目、现场或工程效果。

`city-engraving.webp` 使用内置 image_gen 生成，1536×1024。暖米色纸面、深绿和暗朱红版画主题构图，表现山林、水库、水系与小村镇；不描绘具体真实项目，使用时明确标识“AI 生成 · 主题插画”。

网页使用 WebP 压缩版；未裁剪或改变图像内容。原始下载与生成文件保留在本机忽略的状态目录，来源 URL 不变。

`mainland-hongkong.webp` 为内置 image_gen 生成的栏目通用主题插画，1448×1086；香港与内地沿海城市隔水相连的概念性合成场景，不代表真实地理布局或新闻现场。原始 PNG 保留于本机生成图片目录；WebP 未裁剪，使用时保留 AI 主题插画替代文本。

`mainland-hongkong-v2.webp` 为保留的简化候选版，页面使用第一版 `mainland-hongkong.webp`；简化候选版：去除密集排线、纸张颗粒和复杂窗格，保留主要城市轮廓、山体、桥梁与渡轮，1448×1086。使用内置 image_gen 编辑生成，非新闻现场照片。

`technology.svg` 为代码绘制的通用芯片线条插画，用于科技新闻无可核实图片或加载失败时的分类兜底；不是新闻现场。社会新闻兜底复用 `mainland-hongkong-v2.webp`，山水分类复用 `city-engraving.webp`。
