# 图片资产管理（图库）实测与接口速查

官方文档（`tmp/` 抓取件，2026-11-11）的提炼 + 真实账户实测。原始抓取件不入库。

**状态标记**：✅ 实测通过 / ⚠️ 与文档不一致或易错 / ◐ 部分验证 / 无标记 = 仅文档依据

## 一、三个服务名，别搞混

| 服务 | 产品线 | 用途 | 本项目 |
|---|---|---|---|
| `ImageManageService` | sms | 上传 / 查询 / 改名收藏 / 删除 | ✅ 4 个接口，SDK 自带 |
| `ImageProcessingService` | **feed** | 尺寸适配（裁剪） | ✅ SDK 自带，走 `/json/feed/v1/` |
| `ImageManagementService` | sms | 分享图片、视频抽帧 | ◐ `shareImage` 手写补入；`getVideoExtractImage` SDK 自带 |

> `ImageManagementService`（带 **ment**）≠ `ImageManageService`。参数与用途都不同，混用会报 `87091`。

## 二、上传 `ImageManageService.uploadImage` ✅

```jsonc
{"items": [{"content": "<纯 base64>", "imageName": "可选名"}], "addImage": true}
```

| 要点 | 实测 / 文档 |
|---|---|
| 单次张数 | 最多 **20 张** |
| 请求体大小 | ≤ **10MB** |
| `content` | **纯 base64**，不要带 `data:image/jpg;base64,` 前缀 |
| `addImage` | `true` 才进图库并生成 `imageId`；`false` 只上传不留存（**不返回 imageId**） |
| **去重** | ⚠️ **按内容 MD5 去重**：同一张图重复上传返回**同一个 imageId** |
| 返回 | `imageId / url / width / height / size / signature` |

实测：1×1 透明 PNG（70 字节）→ `imageId 24365854069`、`url https://feed-image.baidu.com/0/pic/…png`、
`size 70`；同内容二次上传仍返回 `24365854069`。

> ⚠️ **本接口不校验宽高/比例**：1×1 PNG（70B）、800×800 商品主图都上传成功，接口只回报 `width/height`，不做判定。
> 尺寸合规是**投放时**由创意/计划接口校验的——例如电商计划 `material.pictures` 要求商品推广
> 370×245 / 1140×640 / 750×1000（非标尺寸报 `901217`，见 `docs/campaign-shopping.md` 第二节）。
> 所以「上传成功」不等于「能用」，别把它当尺寸合规的凭证；`size` 是字节数，不是尺寸。

## 三、查询 `ImageManageService.getImageList` ✅

```jsonc
{"filters": [{"field": "imageId", "op": "eq", "values": ["24365854069"]}],
 "pageNo": 1, "pageSize": 20}
```

| 要点 | 说明 |
|---|---|
| 返回结构 | ⚠️ **单元素数组包一层**：`[{listData: [...], totalCount, pageNo}]` → 取数用 `out[0].listData` |
| `filters` | `{field, op, values}`；op：`eq / in / ge / gt / le / lt / bt` |
| 可筛字段 | `imageId`（eq/in）、`width`/`height`/`size`（eq/in/ge/gt/le/lt/bt）、`format`、`resolution`（`"960*640"`）、`addtime`（bt/eq）、`isCollect` |
| `imageName` | **只有上传时设置过才返回**，没设名称的图不返回该字段 |
| 分页 | `pageNo` 默认 1；`pageSize` 默认 40，范围 (0, 1000) |
| ⚠️ **最终一致** | 刚上传/刚改名后立即查询，可能读到**旧值甚至空列表**（实测改名后立刻查仍是旧名） |

## 四、改名 / 收藏 `ImageManageService.modImage` ✅

```jsonc
{"items": [{"imageId": 24365854069, "imageName": "新名字", "collect": true}]}
```

- `imageId` 必填；`collect=true` 收藏、`false` 取消收藏；`imageName` ≤200 字符（1 汉字算 2）
- ⚠️ 改完立即查列表可能还是旧值（最终一致），别把回读结果当失败

## 五、删除 `ImageManageService.delImage` ✅

```jsonc
{"imageIds": [24365854069]}
```

- 长度 **[1, 100]**；返回被删的 `imageId` 数组
- **无部分成功**：一张删失败，整体算失败
- ⚠️ schema 里还有 `picIds` / `urlList` 两个入口：
  - `urlList` 实测报 **`9015 参数异常`**（没打通）
  - `picIds` **未验证**——`getImageList` 请求 `picId` 字段也不会返回它

## 六、尺寸适配 `ImageProcessingService.addCropImage` ◐（本账户验证失败）

```jsonc
{"srcUrl": "https://…", "width": 370, "height": 245, "desc": "图片描述"}
```

- 走 **feed 产品线**：`/json/feed/v1/ImageProcessingService/addCropImage`
- `srcUrl` 必须是图库已有图片；**目标宽高不得大于原图**
- 等比例居中裁剪缩放，结果存入图库

**实测（失败）**：用 `ImageManageService.uploadImage` 上传的 40×40 图 URL 调本接口
→ `9124015 图片不存在`。说明它**只认 feed 图库的图片**，而本账户是 sms 电商线。
想绕过也不行：上传时 `needMola=true` 报 `901301 get mola URL failed`（拿不到 molaUrl）。

**结论：本账户无法验证此接口**，需要用 feed 线账户（信息流）才能测。

## 七、分享 `ImageManagementService.shareImage` ◐（手写补入，未实测）

```jsonc
{"images": [{"imageId": 1, "targetUserId": "对方账户ID", "imageName": "…"}],
 "shareType": "IMAGE", "productLine": "…"}
```

- `shareType`：`IMAGE`（图库图片）/ `HUDONG`（互动图）
- 该接口**不在 SDK 覆盖范围**，由 `scripts/handwritten_capabilities.json` 补入；
  参数取自官方文档，**尚未实测**（需要另一个账户 ID 才能验证分享效果）

## 八、图片对象字段（`ImageType`）

| 字段 | 说明 |
|---|---|
| `imageId` | 图片 ID（去重后同一内容同 ID） |
| `url` | 预览 URL（不保证唯一） |
| `width` / `height` / `size` | 宽 / 高 / 字节数 |
| `format` | `JPG` / `PNG`（返回大写） |
| `signature` | 图片 MD5 |
| `sourceType` | 来源：0/1 本地上传、3–8/14/16 图片工具、9 版权图片、10 站点挖掘、11 视频抽帧 |
| `isCollect` | 是否收藏 |
| `addTime` / `modTime` | 上传 / 修改时间（返回形如 `Oct 31, 2025 7:33:14 PM`） |
| `imageName` | 名称（仅设置过才返回） |

## 九、实测记录（2026-11-11）

| 操作 | 结果 |
|---|---|
| `uploadImage`（1×1 PNG，addImage=true） | ✅ imageId 24365854069 / 70B / feed-image URL |
| 同内容重复上传 | ✅ 返回同一 imageId（MD5 去重） |
| `modImage` 改名 | ✅ 响应回显新名；列表回读受最终一致影响 |
| `getImageList` 分页 | ✅ 默认 5 条/页正常 |
| `getImageList` filters（imageId eq/in，字符串或数字） | ✅ 均命中 |
| `delImage` | ✅ 返回 [24365854069]，再查为 0 条 |
| `uploadImage` 传 `productLine` / `needMola` | ❌ 分别报 `1519` / `901301`，**这两个参数不可用** |
| `addCropImage`（sms 图 URL） | ❌ `9124015 图片不存在`（只认 feed 图库） |
| `delImage` 的 `urlList` | ❌ `9015 参数异常` |
| 测试图 | 已全部删除，图库无 `mcp-probe*` 残留 |

## 十、还没验证的

| 项 | 状态 / 需要什么 |
|---|---|
| `addCropImage` | ❌ 本账户验证失败（只认 feed 图库）；需要 **feed 线账户** |
| `shareImage` | 未验证；需要**目标账户 ID** |
| `delImage` 的 `picIds` | 未验证；`getImageList` 不返回 picId |
| `delImage` 的 `urlList` | ❌ 实测 `9015`，不可用 |
| 单次 20 张 / 10MB 上限 | 未压测 |
