//===- FooWires.cpp - Flatten Memory Pass ----------------------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// This file defines the FooWires pass.
//
//===----------------------------------------------------------------------===//

#include "PassDetails.h"
#include "circt/Dialect/FIRRTL/FIRRTLAnnotations.h"
#include "circt/Dialect/FIRRTL/FIRRTLOps.h"
#include "circt/Dialect/FIRRTL/FIRRTLTypes.h"
#include "circt/Dialect/FIRRTL/FIRRTLUtils.h"
#include "circt/Dialect/FIRRTL/Passes.h"
#include "mlir/IR/ImplicitLocOpBuilder.h"
#include "llvm/ADT/TypeSwitch.h"
#include "llvm/Support/Debug.h"
#include <numeric>

#define DEBUG_TYPE "lower-memory"

using namespace circt;
using namespace firrtl;

namespace {
    // A test pass that simply replaces all wire names with foo_<n>
    struct FooWires : FooWiresBase<FooWires> {
      void runOnOperation() override;
    };
}

// Runs the pass when triggered by a tool or by circt-opt
void FooWires::runOnOperation() {
  int nWires = 0; // Counts the number of wires modified
  getOperation().walk([&](WireOp wire) { // Walk over every wire in the module
    wire.setName("foo_" + std::to_string(nWires)); // Rename said wire
    nWires++;
  });
}

std::unique_ptr<mlir::Pass> circt::firrtl::createFooWiresPass() {
  return std::make_unique<FooWires>();
}