a = True
b = True

# Yes:
# print(a is b)  # True
# No:
# print(a == b)  # True

# Yes:
# def foo(a, b=None):
#     if b is None:
#         b = []
# No:
# def foo(a, b=[]):
# def foo(a, b=time.time()):
# def foo(a, b=FLAGS.my_thing):

if not a and a is not None:
    pass

c = " "
if c:
    print("C is ' ' !")