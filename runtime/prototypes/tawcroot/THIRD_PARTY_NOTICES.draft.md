# tawcroot 第三方署名草稿（5040 原型，不合入）

本原型使用 [Sophie Winter 的 tawc](https://github.com/wmww/tawc)，固定提交为 `a49a4075228f4801dadc1eee584aed33fe8ea4ce`，源码归档 SHA256 为 `74487243bde86c303e786e79cd4a13e98c3279cae4ab7725dcf5fa51c0b530c1`。

采用范围为该提交 `tawcroot/src` 的生产 C/汇编文件及 `tawcroot/include` 头文件，用于隔离开发探针的 launcher。上游源码未修改；本项目新增固定来源/离线缓存入口，沿用已有 NDK，复用上游生产编译配方，将输出改放默认不打包的目录，并增加显式 debug 接线和身份清单。

上游 [LICENSE.MIT](https://github.com/wmww/tawc/blob/a49a4075228f4801dadc1eee584aed33fe8ea4ce/LICENSE.MIT) 声明：

> Copyright (c) 2026 Sophie Winter

适用许可为 MIT。构建器从已验证的固定归档复制该许可**完整原文**至 `assets/tawcroot-prototype/LICENSE.MIT`，与本草稿及 launcher 一起进入显式启用的 debug APK；原始著作权及许可声明必须保留。

本次不链接 tawc 测试使用的 cleat、STC 或完整 TAWC App 的其他依赖；本说明没有把该 App 的全部组件归为 MIT。原型采用无 libc 动态依赖的上游生产配方，构建后仍审计 DT_NEEDED。未来若改变链接内容或引入其他第三方代码，应按实际内容重新核对并更新正式 notices。本文为原型署名草稿，不表示产品接入或兼容性已获批准。
