# Python 语法要点 — 从 s02/code.py 和 pathlib 源码出发

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
