# Python 语法要点 — 从 s02/s02_code.py 和 pathlib 源码出发

## 1. `any()` + 生成器表达式

```python
# any(条件 for 元素 in 可迭代对象)
dangerous = ["rm -rf /", "sudo", "shutdown"]
if any(d in command for d in dangerous):
    return "Error: Dangerous command blocked"
```

- **生成器表达式** `for d in dangerous` 逐个产出元素
- **条件** `d in command` 检查子串
- **`any()`** 遇到第一个 `True` 就短路返回，全假才返回 `False`
- 兄弟函数 `all()` — 全部为真才返回 `True`

## 2. `pathlib.Path` 的 `/` 运算符和 `.resolve()`

```python
WORKDIR = Path.cwd()
path = (WORKDIR / p).resolve()
```

- **`/`** — Path 重载了运算符，效果等同 `os.path.join()`
- **`.resolve()`** — 解析为绝对路径，消除 `.`、`..` 和符号链接
- 两者配合：`resolve()` 之后再 `is_relative_to()` 检查，防止 `..` 逃逸攻击

## 3. `Path()` 是工厂类，不是真实类型

```python
# Path.__new__ 把自己替换为平台对应的子类
def __new__(cls, *args, **kwargs):
    if cls is Path:
        cls = WindowsPath if os.name == 'nt' else PosixPath
    return object.__new__(cls)
```

- `Path("...")` 实际创建的是 `WindowsPath` 或 `PosixPath` 实例
- 你永远拿不到 `Path` 自己的实例
- `Path.cwd()` 里 `cls(cwd)` 同样走这个流程

### 类层次

```
PurePath              ← 抽象基类，纯路径拼接，不做 IO
  ├── PurePosixPath
  └── PureWindowsPath

Path(PurePath)        ← 工厂类，加了 IO 方法
  ├── PosixPath(Path, PurePosixPath)
  └── WindowsPath(Path, PureWindowsPath)
```

## 4. `@classmethod` — 类方法

```python
@classmethod
def cwd(cls):          # cls 自动 = Path（类本身，不是实例）
    cwd = os.getcwd()
    path = cls(cwd)    # cls(cwd) 等价于 Path(cwd)，触发 __new__ + __init__
    return path
```

- `@classmethod` 只是改变第一个参数（传类而非实例）
- **它本身不触发 `__new__`**，触发构造的是方法体内的 `cls(...)` 调用

## 5. Python 对象创建两步走

```
cls(cwd)
  ├── ① __new__(cls)       → 分配内存，返回裸对象
  └── ② __init__(self)     → 初始化属性
```

- `object.__new__(cls)` — 最底层内存分配（C 级别）
- `cls(...)` — 完整流水线（自动调度 ①+②）

## 6. `class` 体是直接执行的代码

```python
class Path(PurePath):
    if pwd:                              # ← 模块导入时立即执行
        def owner(self):
            return pwd.getpwuid(...)
    else:
        def owner(self):
            raise UnsupportedOperation(...)
```

- `class` 缩进块在模块首次导入时**逐行执行**
- 可以在里面写 `if`、`for`、赋值、甚至 `print`
- 作用：编译时决定方法实现，而非每次调用时判断

## 7. 模块只执行一次（`sys.modules` 缓存）

- 第一次 `import` → 执行模块全部代码（包括所有 class 体）
- 后续 `import` → 直接从 `sys.modules` 取缓存，不再执行
- 例外：`importlib.reload()` 强制重新执行

## 8. 类变量 vs 实例变量

| | 定义位置 | 存储位置 | 是否共享 |
|---|---|---|---|
| **类变量** | class 体内直接赋值 | `类.__dict__` | ✅ 所有实例共享 |
| **实例变量** | `__init__` 中 `self.x = ...` | `实例.__dict__` | ❌ 各自独立 |

```python
class Dog:
    species = "Canis familiaris"   # 类变量

    def __init__(self, name):
        self.name = name            # 实例变量
```

### 关键陷阱：赋值遮蔽 vs 原地修改

```python
# 赋值 → 在实例上新建属性，"遮蔽"类变量，不影响他人
d1.species = "哈士奇"    # 只影响 d1

# 原地修改可变对象 → 影响所有人
d1.members.append("张三")  # list 可变，所有人共享同一个对象
```

| 操作 | 语句 | 影响范围 |
|------|------|----------|
| 赋值（遮蔽） | `inst.x = 新值` | 仅当前实例 |
| 原地修改 | `inst.x.append()` 等 | 所有实例 |
| 通过类修改 | `Klass.x = 新值` | 所有实例 |

### 防御策略

- 需要不同值 → 放 `__init__` 里做实例变量（物理隔离）
- 需要共享值 → 用不可变类型做类变量（无法原地修改，赋值只遮蔽自己）

## 9. `@property` — 劫持属性存取

```python
class Dog:
    _species = "Canis familiaris"

    @property
    def species(self):              # getter — 读
        return self._species

    @species.setter
    def species(self, value):       # setter — 写
        self._species = value

    @species.deleter
    def species(self):              # deleter — 删
        del self._species
```

- **只写 `@property` 不写 setter → 只读属性**
- 普通属性直接操作 `__dict__`，`@property` 是**描述器协议**劫持这个过程
- pathlib 中 `drive`、`root`、`name`、`stem`、`suffix` 全是只读 property

## 10. `__slots__` — 用固定槽位替代 `__dict__`

```python
class PurePath:
    __slots__ = (
        '_raw_paths',
        '_drv', '_root', '_tail_cached',
        '_str',
        '_str_normcase_cached',
        '_parts_normcase_cached',
        '_hash',
    )
```

- **作用**：禁止实例的 `__dict__`，用 C 结构体式固定槽位存属性
- **好处**：省内存（~144 字节/实例 → ~8 字节/槽）、属性访问更快
- **代价**：不能动态添加属性
- **适用场景**：属性集合在定义时就确定的类（如 pathlib — 项目中成千上万个实例）

### 继承规则

```python
class Parent:
    __slots__ = ('x', 'y')

class Child(Parent):
    __slots__ = ('z',)       # 子类实例 = 父类槽(x,y) + 子类槽(z)

class Leaf(Parent):
    __slots__ = ()            # 不新增槽，但继承父类槽。关键：拒绝 __dict__

class Loose(Parent):
    pass                      # 没写 __slots__ → 有父类槽 + __dict__（恢复动态属性）
```

## 11. `__dict__` 与 `__slots__` 的关系（不对称）

| | 有 `__dict__` | 无 `__dict__` |
|---|---|---|
| **有 `__slots__`** | 需显式写 `__slots__ = ('x', '__dict__')` | 默认情况 |
| **无 `__slots__`** | 普通用户类（默认） | 内置类型、C 扩展（如 `int`、`str`） |

- **有槽 → 通常无 dict**（除非显式加 `'__dict__'`）
- **无 dict → 不一定有槽**（内置类型和 C 扩展也没有 dict）

## 12. 值传递 vs 引用传递 — 其实是「传对象引用」

### 结论先行

Python 两者都不是：传参时拷贝的是**引用（对象地址）**，既不是对象本身的拷贝（不是纯值传递），也不是变量本身（不是纯引用传递）。官方术语：**call by object reference**（传对象引用）。

### 变量是「名字」，不是「盒子」

```python
a = [1, 2, 3]      # 右边创建对象，a 只是贴在对象上的标签
b = a              # 第二张标签，贴同一个对象
id(a) == id(b)     # True
```

赋值 = 贴标签。`b = a` 没有复制对象，只是让 `b` 指向 `a` 指向的那个对象。

### 函数调用 = 拷贝引用 + 局部名绑定

调用 `f(a)` 时：创建局部名 `x`，让 `x` 与 `a` 指向**同一个对象**（拷贝的是引用）。之后有两种命运：

```python
def f(x):
    x = 100        # ① 换标签：x 撕下来贴到新对象 → 不影响调用方
a = 1
f(a)
print(a)           # 1   ← 看起来像"值传递"

def g(x):
    x.append(4)    # ② 改对象：不换标签，直接改内容 → 双方都看得见
b = [1, 2, 3]
g(b)
print(b)           # [1, 2, 3, 4]   ← 看起来像"引用传递"
```

| 函数内做的事 | 例子 | 影响调用方？ | 本质 |
|---|---|---|---|
| 重新绑定参数 | `x = 100` | ❌ | 局部名换了对象 |
| 原地修改对象 | `x.append(4)` | ✅ | 双方共享同一对象 |
| 先改再换 | `x.append(4); x = []` | 修改生效、换绑不生效 | 两种机制叠加 |

### 与不可变/可变的关系（表象，非本质）

- `int`、`str`、`tuple` **不可变** → 没有原地修改操作 → 函数内只能换标签 → 永远表现成"值传递"
- `list`、`dict`、`set` **可变** → 有原地修改 → 表现成"引用传递"
- 但底层是**同一个机制**，差异只来自对象本身能不能原地改

### 与第 8 节同构：同一个口诀

| 第 8 节（类变量） | 函数传参 |
|---|---|
| `d1.species = "哈士奇"` 赋值遮蔽 | `x = 100` 换标签 |
| `d1.members.append("张三")` 原地修改 | `x.append(4)` 改对象 |

> 口诀：**赋值永远只动当前名字；原地修改会波及所有持有该对象的名字。**

面试一句话版：传的是引用（地址拷贝），不是对象（值拷贝），也不是变量（引用传递）。判断影响范围只看函数里是**换标签**还是**改对象**。

## 13. 字符串：单引号 vs 双引号 — 完全等价

### 结论

Python 不区分：`'hello'` 和 `"hello"` 是同一个东西，零语义差异。没有 C/JS 那种「单引号是字符、双引号是字符串」的区别（Python 没有单独的 char 类型）。

### 唯一的实际差异：转义

```python
"It's ok"          # ✅ 字符串内可直接包含另一种引号，不用转义
'他说"你好"'         # ✅ 反过来也成立
'It\'s ok'         # 合法但没必要（\ 转义同种引号）
```

### 惯例（PEP 8）

- PEP 8 只规定一条：**选一种，保持一致**，具体选哪种是团队/个人偏好
- **单引号** — Google 风格指南等偏好，视觉上更紧凑
- **双引号** — docstring 几乎统一用 `"""`（PEP 257 推荐，即使平时用单引号）
- 核心原则：跟随项目现有风格，别在同一个文件里混用

### 特殊情况

| 场景 | 写法 |
|---|---|
| 多行字符串 | `'''...'''` 或 `"""..."""`（三引号） |
| docstring | 统一 `"""..."""`（PEP 257） |
| f-string | `f'{name}'` / `f"{name}"` 都行 |
| 空字符串 | `''` / `""` 等价 |

> 口诀：单双引号没区别，选一个用到底；需要嵌套时换另一种引号，省掉转义。

## 14. 传「类名」vs 传「字符串」— 引号装的是数据，不是类

### 核心：Python 没有运行前类型校验

- Python 唯一的运行前阶段是**编译**（源码 → 字节码），只查**语法**（`SyntaxError`），不查类型
- `f("MyClass")` 永远不会在预检阶段报错；单引号双引号无区别，都是普通 str 数据
- 类型错误全部在**运行时**才暴露，且报不报错取决于方法内部怎么用

### 传类 vs 传字符串

```python
f(MyClass)      # 传类对象（type 类型）
f("MyClass")    # 传字符串 —— 只是文本数据，Python 不会自动把它解析成类
```

```python
def use_as_type(cls):
    return isinstance(1, cls)

use_as_type(MyClass)     # False，正常
use_as_type("MyClass")   # TypeError: isinstance() arg 2 must be a type...

def instantiate(cls):
    return cls()         # 把字符串当类调用

instantiate("MyClass")   # TypeError: 'str' object is not callable

def store(cls):
    return {"cls": cls}  # 只是存起来

store("MyClass")         # ✅ 永不报错，就是个字符串
```

### 传字符串也可能是设计意图

方法内部主动把字符串解析回类时，传字符串是正常用法：

```python
getattr(module, "MyClass")      # 按名字取类
globals()["MyClass"]()          # 按名字实例化
```

框架里很常见（如 Django 用 `"app.Model"` 字符串引用模型）。此时名字写错 → 运行时 `NameError`/`AttributeError`。

### 注解场景：类名加引号是合法写法（forward reference）

```python
class A:
    def foo(self, x: "B"):   # B 在文件后面才定义
        ...
```

- **不加引号**：`def` 执行时就求值注解 → 立刻 `NameError`（导入阶段就炸）
- **加引号**：注解存成字符串、不求值 → 不炸；mypy/pyright 也能读懂引号里的类名
- Python 3.10+ 用 `from __future__ import annotations`（PEP 563）后所有注解自动变字符串，手写引号就不必要了

### 「运行前校验」是谁做的？

| 工具 | 会不会在运行前查类型 |
|---|---|
| Python 本身 | ❌ 编译只查语法，类型全靠运行时 |
| mypy / pyright（外部工具） | ✅ 编辑器/CI 里静态检查，划红线报 `incompatible type "str"` |

## 15. 字符串前瞻引用（forward reference）

### 是什么

类型注解里把类型名写成字符串，把「求值」推迟到以后。因为注解默认在 `def`/`class` **定义时立即求值**：

```python
def foo(x: MyClass): ...    # def 执行那一刻，立刻求值 MyClass
```

而有些类型在 `def` 执行的那一刻**还不存在**。

### 场景 1：类引用自己（经典）

```python
class Node:
    def connect(self, other: "Node") -> "Node":
        ...
```

不加引号会炸：`class` 体一行行执行，`def connect` 执行时 `Node` 这个名字还不存在（类对象要等整个 class 体跑完才创建）→ `NameError: name 'Node' is not defined`。

### 场景 2：循环导入（TYPE_CHECKING 块标准姿势）

```python
# a.py
from typing import TYPE_CHECKING

if TYPE_CHECKING:            # 只在类型检查时导入，运行时跳过
    from b import B

class A:
    def make_b(self) -> "B":   # 运行时 B 的导入被跳过，注解只能是字符串
        ...
```

### 引号到底做了什么

| 时刻 | 有引号的效果 |
|---|---|
| `def` 定义时 | 存字符串，不求值 ✅ 不炸 |
| 类型检查时 | mypy/pyright 读字符串，照常解析 |
| 运行时需要真实类型 | `typing.get_type_hints()` 用模块 globals 解析回真实类 |

```python
def f(x: "MyClass"): ...
f.__annotations__              # {'x': 'MyClass'} —— 存的是字符串

import typing
typing.get_type_hints(f)       # {'x': MyClass} —— 字符串被解析回真实类型
```

注意：解析发生在**主动要的时候**；到时名字不存在 → 运行时 `NameError`。pydantic/FastAPI 在类创建时就 `get_type_hints()` 解析，对前瞻引用敏感（有 `model_rebuild()` 补救）。

### 两个 PEP 的演进

- **PEP 563**：`from __future__ import annotations` → 所有注解自动变字符串，手写引号不需要。曾计划 3.10 转正为默认，**后被撤回**——把注解全变字符串破坏了 pydantic 等依赖运行时反射的工具
- **PEP 649（Python 3.14+）**：注解默认**懒求值**（访问时才求值，不存字符串）→ 前瞻引用问题基本消失，不加引号也不炸

### 完整可运行示例：方法 + 实际调用

```python
class Node:
    """链表节点：connect 方法注解里引用自己 —— 前瞻引用"""

    def __init__(self, value: int):
        self.value = value
        self.next = None

    def connect(self, other: "Node") -> "Node":
        """把 other 接在自己后面，返回 other。"""
        self.next = other
        return other
```

实际调用（Python 3.14.4 实测输出）：

```python
head = Node(1)
n2 = Node(2)
n3 = Node(3)

head.connect(n2)      # 返回 n2
n2.connect(n3)        # 返回 n3

print(head.value, head.next.value, head.next.next.value)   # 1 2 3
print(head.next is n2, n2.next is n3)                     # True True
```

```
1 2 3
True True
```

注解里存的是字符串，运行时才解析回真实类型：

```python
Node.connect.__annotations__            # {'other': 'Node', 'return': 'Node'} ← 字符串
typing.get_type_hints(Node.connect)     # {'other': <class 'Node'>, ...} ← 真实类型
```

### 实测：3.14 里加不加引号（PEP 649 懒求值）

Python 3.14+ 上不带引号的注解定义时也不炸，但存储方式不同：

```python
class C:
    def f(self, x: "C"): ...   # 带引号
    def g(self, x: C): ...     # 不带引号

C.f.__annotations__   # {'x': 'C'}          ← 存的是字符串
C.g.__annotations__   # {'x': <class C>}    ← 访问时懒求值出真实类型
```

- **3.13 及更早**：不带引号 → 定义时就 `NameError`（class 体执行到 `def` 那行，类还没创建），引号是必需品
- **3.14+**：PEP 649 懒求值，不带引号也不炸；带引号则存字符串，行为与旧版一致（兼容老代码）

> 小结：前瞻引用 = 用字符串把类型名的求值推迟；旧版本必需品，3.14 起 Python 自己懒求值。

## 16. `__annotations__` — 注解存在哪里

一句话：函数、类、模块各有一个 `__annotations__` 字典，类型注解（参数、返回、属性）就存在里面。实例没有。

### 存储位置（Python 3.14.4 实测）

| 位置 | 写法 | 结果 |
|---|---|---|
| 函数参数 + 返回 | `def f(x: int) -> str` | `f.__annotations__` = `{'x': int, 'return': str}`（`'return'` 是特殊键） |
| 类属性 | `attr: int = 5` | `C.__annotations__` = `{'attr': int}` |
| 模块级变量 | `count: int = 0` | 3.13 及更早：模块全局 `__annotations__`；3.14+ 见下方变化 |
| 函数体内局部变量 | `x: int = 5` | ❌ 不入任何字典（`g.__annotations__` 仍是 `{}`） |
| 方法内 | `self.x: int = 0` | ❌ 求值后丢弃；实例没有 `__annotations__` |

### 关键性质

- **普通 dict，可写可改**：`f.__annotations__["return"] = float` —— 运行时反射、动态改造依赖这点
- 没写注解 → 空字典 `{}`
- 字符串注解**原样存字符串** —— 第 15 节前瞻引用的存储形态
- 读取/解析推荐 `typing.get_type_hints()` 或 `inspect.get_annotations()`，别直接碰原始字典

### 3.14 的变化（PEP 649 懒求值）

- 3.14 前：注解在 def/class 定义时立即求值写入字典
- 3.14 起：函数/类的 `__annotations__` 仍在（访问时才计算），但**模块级不再有 `__annotations__` 全局变量**，改为 globals 里的 `__annotate__()` 函数（实测 `'__annotations__' in globals()` → `False`）
- 附带效果（实测）：函数体内局部注解 `y: Nope = 5` 引用未定义名**不执行也不炸**——注解压根不求值

## 17. 泛型 `X[T]` — 方括号不是 list，是「参数化类型」

### 结论

`def get_final_message(self) -> ParsedMessage[ResponseFormatT]` 里的 `ParsedMessage[ResponseFormatT]` **不是 list**，而是**泛型订阅（generic subscripting）**：用类型参数 T 把泛型类具体化成一个更精确的**类型**。读作「解析结果为 T 的 ParsedMessage 类型」。

### 源码证据（anthropic SDK）

```python
# lib/_parse/_response.py:13
ResponseFormatT = TypeVar("ResponseFormatT", default=None)   # 类型变量：占位符

# types/parsed_message.py:57
class ParsedMessage(Message, Generic[ResponseFormatT]):       # 泛型类
```

- `ResponseFormatT` 是 **TypeVar 类型变量**，等着被具体类型替换
- `Generic[ResponseFormatT]` 声明 `ParsedMessage` 是泛型类
- 于是类内注解可以写 `ParsedMessage[ResponseFormatT]`

### 类比 `list[int]`

```python
text_blocks: list[str] = []   # _messages.py:106 —— 这才是"列表"：元素为 str 的 list 的类型
```

- `list[int]` 是「元素为 int 的 list 的**类型**」—— 是类型对象（`types.GenericAlias`），不是 list 实例
- `ParsedMessage[T]` 同理是**类型**，不是实例。实测（pydantic v2）泛型订阅会直接生成具体化的类，比标准库更彻底：

```python
type(ParsedMessage[int])   # <class 'pydantic...ModelMetaclass'> —— 具体化的类，可直接 ParsedMessage[int]()
type(list[int])            # types.GenericAlias —— 标准库泛型返回类型别名对象
isinstance(ParsedMessage[int], list)   # False
```

### 三个证据：`get_final_message()` 返回的是单对象

1. `self.__final_message_snapshot: ParsedMessage[ResponseFormatT] | None` —— 一个「快照」对象
2. `accumulate_event(...) -> ParsedMessage[ResponseFormatT]` —— 累积的是**单条**消息
3. 同文件真正的列表写法：`list[ParsedMessageStreamEvent[ResponseFormatT]]` —— 带 `list` 字样的才代表列表

### 三种括号用法别混淆

| 写法 | 含义 | 场景 |
|---|---|---|
| `x[0]` | 下标取元素 | 表达式（运行时） |
| `ParsedMessage[T]` / `list[int]` | 泛型参数化 | 类型注解（类型层面） |
| `f(1, 2)` | 调用函数 | 表达式 |

### 与笔记的联动

- 文件第一行 `from __future__ import annotations` → 这些注解运行时全是字符串（§15/§16）
- `ParsedMessage[T] | None` 的 `|` 是联合类型 —— 「可能是 ParsedMessage，可能是 None」
- s02 代码里 `stream.get_final_message()` 返回它：传了 `output_format` 时 `parse_text` 用 `TypeAdapter` 把文本 JSON 解析成该类型（`parsed_output`）；没传时默认 `None`

## 18. `TypeAlias` 与 `Union` — 类型别名和联合类型

### 这一行是什么

```python
InputSchema: TypeAlias = Union[InputSchemaTyped, Dict[str, object]]   # tool_param.py:30
```

三部分：名字 `InputSchema` + 标注 `: TypeAlias`（声明这是类型别名）+ 值 `= Union[...]`（一个联合类型）。

### `TypeAlias`（PEP 613）

- 特殊标注，**运行时零行为**，只对类型检查器（mypy/pyright）有意义
- 作用：告诉检查器「这个赋值声明的是类型别名」。没有它，mypy 会把 `InputSchema` 当「类型为 Union 的普通变量」；而别处 `input_schema: Required[InputSchema]` 要把它当类型用 —— 两者矛盾，`TypeAlias` 消歧义
- 从 `typing_extensions` 导入是因为要兼容老版本（`typing.TypeAlias` 3.10+ 才有）
- Python 3.12+ 新写法：`type InputSchema = Union[...]`（PEP 695）

### `Union[A, B]` = 「A 或 B」

- 本行含义：`input_schema` 可以是结构化 TypedDict（`InputSchemaTyped`），也可以是任意 `Dict[str, object]`（键 str、值任意 —— 宽松兜底）
- 运行时实测：

```python
InputSchema    # InputSchemaTyped | typing.Dict[str, object]  ← Union 对象
type(...)      # typing.Union —— 类型描述，不是类
InputSchema()  # TypeError: 'typing.Union' object is not callable  ← 不可实例化
```

- pydantic 用这个 Union 做校验；`typing.get_type_hints(ToolParam)["input_schema"]` 能解析出它

### 联动

- `Dict[str, object]` 是 §17 的泛型参数化
- `from __future__ import annotations` → 注解 `TypeAlias` 不求值，但 **RHS 照常求值赋值** —— 注解延迟 ≠ 赋值延迟（§15/§16）
- `TypedDict`：带键名和类型约束的字典「蓝图」；`total=False` 表示键全部可选，`Required[...]` 强制必填

## 19. `TypedDict` + `Required[...]` — 带类型的字典蓝图

### 这一行是什么

```python
class ToolParam(TypedDict, total=False):
    input_schema: Required[InputSchema]    # tool_param.py:34
```

拆解：`input_schema` 是键名；`Required[...]` 是「必填」标记（PEP 655）；`InputSchema` 是值的类型（§18 的别名 `InputSchemaTyped | Dict[str, object]`）。

### TypedDict（PEP 589）

- 带键名和类型约束的**字典蓝图**：类型检查器按它检查字典的键和值类型
- 默认 `total=True`：所有键必填
- `total=False`：所有键默认可选

### Required / NotRequired（PEP 655）

| 标记 | 场景 | 含义 |
|---|---|---|
| `Required[X]` | `total=False` 的类里 | 把 X 键改回**必填** |
| `NotRequired[X]` | `total=True` 的类里 | 把 X 键改成**可选** |

- 仅影响类型检查器：漏写必填键 → mypy/pyright 报错；可选键漏写不报
- 运行时实测：

```python
Required[int]      # typing.Required[int] —— _GenericAlias 标记对象，不是类不是值
typing.get_type_hints(ToolParam)["input_schema"]
# → InputSchemaTyped | typing.Dict[str, object] —— Required 标记被剥掉，只剩类型
```

- 纯检查器概念：不提供运行时校验（校验靠 pydantic 等工具）

### 联动

- `InputSchema` = §18 类型别名；`Union`/`Dict[str, object]` = §17/§18
- 蓝图类对比：**TypedDict 是字典的蓝图**；dataclass / `__slots__` 类是**对象的蓝图**

## 20. `Annotated[T, 元数据]` — 给类型挂「说明书」

### 这一行是什么

```python
# parsed_beta_message.py:43
ParsedBetaContentBlock: TypeAlias = Annotated[
    Union[ParsedBetaTextBlock[ResponseFormatT], BetaThinkingBlock, ...],  # 16 种内容块
    PropertyInfo(discriminator="type"),   # 元数据
]
```

拆解：
- `Annotated[T, m1, m2, ...]`（PEP 593）：**第一个参数是类型 T，后面跟任意元数据**
- 分工：**类型检查器只看 T**（16 选 1 的联合）；**元数据给运行时工具看**（这里是 pydantic）
- 运行时实测：`ParsedBetaContentBlock` 是 `typing._AnnotatedAlias` 对象 —— 仍是类型描述，不是类不是实例

### `PropertyInfo(discriminator="type")` — 判别联合（tagged union）

- 元数据告诉 pydantic：**按 JSON 里的 `type` 字段的值**决定反序列化成 16 个成员中的哪一个
- `{"type": "text", ...}` → `ParsedBetaTextBlock`；`{"type": "tool_use", ...}` → `BetaToolUseBlock` —— SDK 的「判别式反序列化」机制
- pydantic 原生等价写法（实测 ✅ 按 `type` 正确派发）：

```python
adapter = TypeAdapter(Annotated[Union[Text, ToolUse], Field(discriminator="type")])
adapter.validate_json('{"type":"text","text":"hi"}')      # → Text
adapter.validate_json('{"type":"tool_use","name":"ls"}')  # → ToolUse
```

### 为什么不能直接 `Union[...]`

文件注释：**generic unions are not valid for pydantic at runtime** —— 联合里含 `ParsedBetaTextBlock[ResponseFormatT]` 这种泛型参数化成员（§17），pydantic 运行时无法直接处理，用 `Annotated` 补上判别信息绕开。

### 联动

- `TypeAlias` = §18；`Union` = §18；`X[T]` / `TypeVar` = §17
- `if TYPE_CHECKING: ... else:` = §15；`from __future__ import annotations` = §15/§16

## 21. `pydantic` — 让类型注解变成运行时校验

### 是什么

pydantic 是最流行的 **Python 数据校验库**：用类型注解声明数据结构，实例化时**自动校验 + 类型转换 + 序列化**。核心是 Rust 写的（pydantic-core），速度很快。anthropic SDK 的模型层（`ParsedMessage`、`ToolUseBlock`、`ToolParam`…）全建在它上面。

### 核心：`BaseModel`（pydantic 2.13.4 实测）

```python
from pydantic import BaseModel, ValidationError

class User(BaseModel):
    name: str
    age: int

u = User(name="张三", age="25")   # 字符串 "25" → 自动转换
print(u.age, type(u.age))          # 25 <class 'int'>
print(u.model_dump())              # {'name': '张三', 'age': 25} —— 转回 dict
print(u.model_dump_json())         # {"name":"张三","age":25} —— JSON

User(name="李四", age="abc")       # 转不了 → ValidationError
```

- 校验发生在**实例化时**：类型对不上自动转换（`"25"` → `25`），转换不了抛 `ValidationError`
- 序列化：`model_dump()` / `model_dump_json()`

### `TypeAdapter` — 不建模型，直接校验任意类型

```python
TypeAdapter(int).validate_python("42")   # 42 —— §20 判別联合 demo 就是它
```

### `model_construct()` — 跳过校验快速构造

```python
User.model_construct(name="x", age="不是数字")   # 不校验、不转换
```

SDK 流式累积事件时用 `ParsedMessage.construct(...)`（_messages.py:452）就是性能优先、校验后置。⚠️ 实测：`construct` 在 v2.13 已弃用，新代码用 `model_construct`。

### 与 `dataclass` 对比

| | dataclass（标准库） | pydantic BaseModel |
|---|---|---|
| 运行时校验 | ❌ 无 | ✅ 实例化时校验+转换 |
| 序列化 | 手动写 | `model_dump()` / `model_dump_json()` |
| 性能 | — | v2 Rust 核心，很快 |
| 适用 | 简单数据容器 | API/配置等外部数据 |

### 版本

- v1：`.dict()` / `.json()`；**v2**（2023+）：`model_dump()` / `model_dump_json()`，核心 Rust 重写 —— SDK 用 v2

### SDK 里的 pydantic 足迹（复习串联）

- `ParsedMessage(Message, Generic[...])` — BaseModel 子类（§17）
- `construct_type` / `construct_type_unchecked` — 按类型构建（含 §20 判别联合）
- `parse_text` 里 `TypeAdapter(output_format).validate_json(text)` — 把文本按指定类型解析（§17）
- `PropertyInfo(discriminator=...)` — pydantic 判别联合元数据（§20）
