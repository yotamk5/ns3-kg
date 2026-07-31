; Function/method definitions (have a body).
(function_definition
  declarator: [
    (function_declarator) @fn.declarator
    (pointer_declarator (function_declarator) @fn.declarator)
    (reference_declarator (function_declarator) @fn.declarator)
  ]) @fn.node

; Prototypes at namespace scope and constructor declarations in class bodies.
(declaration
  declarator: [
    (function_declarator) @proto.declarator
    (pointer_declarator (function_declarator) @proto.declarator)
    (reference_declarator (function_declarator) @proto.declarator)
  ]) @proto.node

; Method declarations inside class bodies.
(field_declaration
  declarator: [
    (function_declarator) @proto.declarator
    (pointer_declarator (function_declarator) @proto.declarator)
    (reference_declarator (function_declarator) @proto.declarator)
  ]) @proto.node
